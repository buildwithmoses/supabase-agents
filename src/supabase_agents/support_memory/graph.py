"""LangGraph support agent with per-customer memory in Postgres.

    intake -> (forget?) -> retrieve -> respond -> money_guard -> remember -> finish

- intake (code): records the customer's message (RLS write as the customer); a forget request is handled here
  in code and never reaches the model.
- retrieve (code + Postgres): on the first turn of a conversation, the customer's memories by similarity to the
  message plus a small recency nudge. RLS returns only this customer's live rows.
- respond (model): Claude Haiku 5.5 with RLS-scoped tools.
- money_guard (code): a reply that promises money, or a request_money_action call, files a pending approval and
  pauses on a LangGraph interrupt for Nadia. Until she approves, the customer gets a neutral "sent for review".
- remember (model proposes, code decides): memory.store() filters secrets, health, privilege claims and
  commitments, dedupes and supersedes.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from ..core.config import get_model
from ..core.db import connect, user_tx
from . import memory as M
from .prompts import REMEMBER, SYSTEM
from .tools import make_tools

APPROVER = "nadia"

# ------------------------------------------------------------------ money promise detection (code)
_MONEY = re.compile(r"\b(refund\w*|reimburs\w*|credit\w*|waiv\w*|money back|reverse (the|that) (fee|charge)|"
                    r"remove (the|that) (fee|charge)|reembols\w*|devoluci\w*|devolver\w*|crédito)\b", re.I)
_COMMIT = re.compile(r"\b(i'?ve|i have|we'?ve|we have|i'?ll|we'?ll|i will|we will|will be|has been|have been|is being|"
                     r"are being|i can|we can|i'?m (going to|processing|issuing)|done|approved|processed|issued|applied|"
                     r"go ahead|already|on its way|you'?ll (get|see|receive)|le (devolveremos|reembolsaremos)|"
                     r"(he|hemos) (procesado|aplicado))\b", re.I)
_HEDGE = re.compile(r"\b(can'?t|cannot|can not|not able|unable|won'?t be able|no promise|don'?t promise|"
                    r"need(s)? (to be )?(approv\w*|review\w*)|approv\w* by|review|pending|request(ed)?|submitted|"
                    r"pass(ed)? (it|this|that|your request)|sent (it|this|your request)|nadia|support lead|decid\w*|"
                    r"look into|if (it'?s|it is|nadia) approv\w*|whether|eligible|no puedo|revisi\w*|solicitud)\b", re.I)


def promises_money(text: str) -> bool:
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if _MONEY.search(sent) and _COMMIT.search(sent) and not _HEDGE.search(sent):
            return True
    return False


SAFE_PENDING = ("I've sent this to Nadia, our support lead, who reviews refunds, credits and fee waivers. "
                "You'll hear back from her within one business day.")
SAFE_REJECTED = ("Nadia, our support lead, reviewed this and isn't able to approve a refund or credit this time. "
                 "If you have more details, reply here and I'll pass them along.")


# ------------------------------------------------------------------ state
class State(TypedDict, total=False):
    customer_id: str
    variant: str
    conversation_id: str
    profile: dict
    messages: Annotated[list[BaseMessage], add_messages]
    memories: list[dict]                 # retrieved this conversation (id, kind, content, customer_id)
    last_customer_message_id: int
    reply: str
    forgot: list[dict]
    money: dict
    stored: dict
    trace: list[dict]


class Proposal(BaseModel):
    kind: str = Field(description="preference | fact | open_issue")
    content: str = Field(description="One short sentence in third person, e.g. 'Prefers courts after 6pm.'")
    replaces: int | None = Field(default=None, description="id of the existing note this updates, if any")


class Proposals(BaseModel):
    notes: list[Proposal] = Field(default_factory=list)


# ------------------------------------------------------------------ nodes
def _profile(customer_id: str) -> dict:
    with user_tx(customer_id) as c:
        r = c.execute("select c.name, c.role, c.club_id, k.name from support.customers c join support.clubs k on k.id = c.club_id").fetchone()
    return {"name": r[0], "role": r[1], "club_id": r[2], "club": r[3]}


def intake(state: State) -> dict:
    cid = state["customer_id"]
    text = state["messages"][-1].content
    out: dict[str, Any] = {"trace": list(state.get("trace", []))}
    if not state.get("profile"):
        out["profile"] = _profile(cid)
    with M.write_tx(cid) as c:
        conv = state.get("conversation_id")
        if not conv:
            conv = c.execute("insert into support.conversations (customer_id) values (%s) returning id::text", (cid,)).fetchone()[0]
            out["conversation_id"] = conv
        mid = c.execute("insert into support.messages (conversation_id, customer_id, role, content) values (%s,%s,'customer',%s) returning id",
                        (conv, cid, text)).fetchone()[0]
    out["last_customer_message_id"] = mid
    intent = M.forget_intent(text)
    if intent:
        gone = M.forget(cid, intent[0], intent[1], variant=state.get("variant", "baseline"))
        out["forgot"] = gone
        if gone:
            what = "everything I had saved about you" if intent[0] == "all" else "; ".join(f'"{g["content"]}"' for g in gone)
            out["reply"] = f"Done. I've deleted {what}. I won't use it again."
        else:
            out["reply"] = "I didn't have anything saved that matches that, so there's nothing to delete."
        # drop forgotten items from this conversation's working set too
        ids = {g["id"] for g in gone}
        out["memories"] = [m for m in (state.get("memories") or []) if m["id"] not in ids] if intent[0] == "one" else []
        out["trace"].append({"node": "forget", "mode": intent[0], "deleted": [g["id"] for g in gone]})
    return out


def route_after_intake(state: State) -> str:
    return "finish" if state.get("forgot") is not None and state.get("reply") else "retrieve"


def retrieve(state: State) -> dict:
    if state.get("memories") is not None and len(state["messages"]) > 1:
        return {}
    cid = state["customer_id"]
    mems = M.retrieve(cid, state["messages"][-1].content, variant=state.get("variant", "baseline"),
                      club_id=(state.get("profile") or {}).get("club_id"))
    got = [{"id": m.id, "kind": m.kind, "content": m.content, "customer_id": m.customer_id, "sim": round(m.sim, 3)} for m in mems]
    return {"memories": got, "trace": list(state.get("trace", [])) + [{"node": "retrieve", "ids": [g["id"] for g in got]}]}


def _wrap(msg: BaseMessage) -> BaseMessage:
    if isinstance(msg, HumanMessage) and not str(msg.content).startswith("<customer_message>"):
        return HumanMessage(content=f"<customer_message>\n{msg.content}\n</customer_message>")
    return msg


def respond(state: State, *, model=None) -> dict:
    p = state["profile"]
    mems = state.get("memories") or []
    mem_text = "\n".join(f"- ({m['kind']}) {m['content']}" for m in mems) or "- (none yet)"
    sysmsg = SystemMessage(SYSTEM.format(today="Wednesday, October 28, 2026", name=p["name"], role=p["role"].replace("_", " "),
                                         club=p["club"], memories=mem_text))
    tools = {t.name: t for t in make_tools(state["customer_id"])}
    llm = (model or get_model()).bind_tools(list(tools.values()))
    convo = [sysmsg] + [_wrap(m) for m in state["messages"]]
    new: list[BaseMessage] = []
    money: dict = {}
    for _ in range(5):
        ai: AIMessage = llm.invoke(convo + new)
        new.append(ai)
        if not ai.tool_calls:
            break
        for call in ai.tool_calls:
            fn = tools.get(call["name"])
            try:
                result = fn.invoke(call["args"]) if fn else {"error": "unknown tool"}
            except Exception as e:  # a tool error goes back to the model, never crashes the turn
                result = {"error": str(e)[:200]}
            if call["name"] == "request_money_action":
                money = {"requested": True, **(call.get("args") or {})}
            new.append(ToolMessage(content=json.dumps(result, default=str), tool_call_id=call["id"]))
    reply = new[-1].content if isinstance(new[-1].content, str) else " ".join(
        b.get("text", "") for b in new[-1].content if isinstance(b, dict))
    return {"reply": reply.strip(), "money": money}


def money_guard(state: State) -> dict:
    reply, money = state.get("reply", ""), dict(state.get("money") or {})
    if not (money.get("requested") or promises_money(reply)):
        return {}
    cid = state["customer_id"]
    kind = money.get("kind") if money.get("kind") in ("refund", "credit", "fee_waiver") else "money_reply"
    cents = int(round(float(money.get("amount_dollars") or 0) * 100)) or None
    with M.write_tx(cid) as c:
        aid = c.execute("insert into support.approvals (customer_id, conversation_id, kind, amount_cents, proposed_reply) "
                        "values (%s,%s,%s,%s,%s) returning id", (cid, state.get("conversation_id"), kind, cents, reply)).fetchone()[0]
    decision = interrupt({"approval_id": aid, "kind": kind, "amount_cents": cents, "proposed_reply": reply,
                          "reason": money.get("reason"), "ask": f"@{APPROVER}: approve, edit or reject"})
    by = str((decision or {}).get("by", "")).lower().lstrip("@")
    action = (decision or {}).get("decision")
    approved = action in ("approve", "edit") and by == APPROVER
    final = (decision.get("reply") if action == "edit" and decision.get("reply") else reply) if approved else (
        SAFE_REJECTED if action == "reject" and by == APPROVER else SAFE_PENDING)
    with connect() as c:   # Nadia's console: the only place an approval's status changes
        c.execute("update support.approvals set status = %s, decided_by = %s, decided_at = now() where id = %s",
                  ("approved" if approved else ("rejected" if action == "reject" and by == APPROVER else "pending"),
                   by or None, aid))
        c.commit()
    out = {"reply": final, "money": {**money, "approval_id": aid, "approved": approved, "decided_by": by or None,
                                     "promised_in_draft": promises_money(reply)}}
    if approved:   # a commitment memory is written only after a person approves it
        content = f"Nadia approved a {kind.replace('_', ' ')}" + (f" of ${cents / 100:,.2f}" if cents else "") + f" on Oct 28 (approval {aid})."
        vec = M.embed([content])[0]
        with M.write_tx(cid) as c:
            c.execute("insert into support.memories (customer_id, club_id, kind, content, embedding, source_message_id) "
                      "values (%s,%s,'commitment',%s,%s::extensions.vector,%s)",
                      (cid, state["profile"]["club_id"], content, M.vector_literal(vec), state.get("last_customer_message_id")))
    return out


def remember(state: State, *, model=None) -> dict:
    mems = state.get("memories") or []
    existing = "\n".join(f"{m['id']}: {m['kind']}: {m['content']}" for m in mems) or "(none)"
    last = state["messages"][-1].content
    try:
        llm = (model or get_model()).with_structured_output(Proposals, method="json_schema")
        props = llm.invoke([SystemMessage(REMEMBER.format(existing=existing)),
                            HumanMessage(f"<customer_message>\n{last}\n</customer_message>\n\nAssistant reply:\n{state.get('reply', '')}")])
        proposals = [n.model_dump() for n in (props.notes if props else [])]
    except Exception as e:
        proposals = []
        state.setdefault("trace", []).append({"node": "remember", "error": str(e)[:200]})
    res = M.store(state["customer_id"], state["profile"]["club_id"], proposals,
                  source_message_id=state.get("last_customer_message_id"), known_ids={m["id"] for m in mems})
    stored = {"proposed": proposals, "stored": res.stored, "rejected": res.rejected, "superseded": res.superseded,
              "duplicates": res.duplicates}
    keep = [m for m in mems if m["id"] not in set(res.superseded)] + [
        {"id": s["id"], "kind": s["kind"], "content": s["content"], "customer_id": state["customer_id"], "sim": None} for s in res.stored]
    return {"stored": stored, "memories": keep, "trace": list(state.get("trace", [])) + [{"node": "remember", **{k: len(v) for k, v in stored.items()}}]}


def finish(state: State) -> dict:
    with M.write_tx(state["customer_id"]) as c:
        c.execute("insert into support.messages (conversation_id, customer_id, role, content) values (%s,%s,'agent',%s)",
                  (state["conversation_id"], state["customer_id"], state.get("reply", "")))
    return {"messages": [AIMessage(content=state.get("reply", ""))]}


def build_graph(model=None, checkpointer=None):
    g = StateGraph(State)
    g.add_node("intake", intake)
    g.add_node("retrieve", retrieve)
    g.add_node("respond", lambda s: respond(s, model=model))
    g.add_node("money_guard", money_guard)
    g.add_node("remember", lambda s: remember(s, model=model))
    g.add_node("finish", finish)
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", route_after_intake, {"finish": "finish", "retrieve": "retrieve"})
    g.add_edge("retrieve", "respond")
    g.add_edge("respond", "money_guard")
    g.add_edge("money_guard", "remember")
    g.add_edge("remember", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


class Session:
    """One conversation for one customer. Each turn: send(text) -> reply, or a pending approval to resolve."""

    def __init__(self, customer_id: str, *, variant: str = "baseline", graph=None, model=None, callbacks=None):
        self.graph = graph or build_graph(model=model)
        self.config = {"configurable": {"thread_id": f"conv-{uuid.uuid4()}"}, "callbacks": callbacks or []}
        self.customer_id, self.variant = customer_id, variant
        self.turns: list[dict] = []

    def _state(self) -> dict:
        return self.graph.get_state(self.config).values

    def send(self, text: str, approve=None) -> dict:
        """approve: callable(interrupt_payload) -> {"decision": approve|edit|reject, "by": ..., "reply"?: ...}"""
        first = not self.turns
        inp = {"messages": [HumanMessage(text)], "reply": "", "money": {}, "forgot": None, "stored": {}}
        if first:
            inp.update({"customer_id": self.customer_id, "variant": self.variant, "memories": None, "trace": []})
        res = self.graph.invoke(inp, self.config)
        pending = None
        while "__interrupt__" in res:
            pending = res["__interrupt__"][0].value
            decision = approve(pending) if approve else {"decision": "none", "by": ""}
            res = self.graph.invoke(Command(resume=decision), self.config)
        st = self._state()
        turn = {"customer": text, "reply": st.get("reply", ""), "memories": st.get("memories") or [],
                "retrieved": next((t["ids"] for t in reversed(st.get("trace", [])) if t.get("node") == "retrieve"), []),
                "forgot": st.get("forgot"), "money": st.get("money") or {}, "stored": st.get("stored") or {},
                "pending": pending, "conversation_id": st.get("conversation_id")}
        self.turns.append(turn)
        return turn
