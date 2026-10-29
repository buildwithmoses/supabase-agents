"""Chat with the support-memory agent as a fictional Courtline customer.

    uv run python -m supabase_agents.support_memory.cli seed
    uv run python -m supabase_agents.support_memory.cli chat maya            # interactive; blank line = new session
    uv run python -m supabase_agents.support_memory.cli say ben "Please refund my late fee" --approve nadia
    uv run python -m supabase_agents.support_memory.cli memories maya        # what Postgres returns for maya (RLS)
"""

from __future__ import annotations

import argparse
import json

from .graph import APPROVER, Session
from .memory import retrieve
from .seed import CUSTOMERS, cid, reset_and_seed


def _approver(who):
    def decide(payload):
        print(f"\n[approval needed] {json.dumps({k: payload[k] for k in ('approval_id', 'kind', 'amount_cents')})}")
        print(f"  proposed reply: {payload['proposed_reply']}")
        if who:
            return {"decision": "reject" if who == "reject" else "approve", "by": APPROVER if who in ("nadia", "reject") else who}
        ans = input(f"  {APPROVER}, approve? [a]pprove / [r]eject / [s]kip: ").strip().lower()
        return {"decision": {"a": "approve", "r": "reject"}.get(ans[:1], "none"), "by": APPROVER if ans[:1] in "ar" and ans else ""}
    return decide


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    c = sub.add_parser("chat"); c.add_argument("who", choices=[h for h, *_ in CUSTOMERS])
    s = sub.add_parser("say"); s.add_argument("who"); s.add_argument("text"); s.add_argument("--approve", default=None)
    m = sub.add_parser("memories"); m.add_argument("who"); m.add_argument("--query", default="anything")
    a = ap.parse_args()
    if a.cmd == "seed":
        print(reset_and_seed())
    elif a.cmd == "memories":
        for mem in retrieve(cid(a.who), a.query, k=20):
            print(f"{mem.id:>4} {mem.kind:11s} sim={mem.sim:.2f}  {mem.content}")
    elif a.cmd == "say":
        t = Session(cid(a.who)).send(a.text, approve=_approver(a.approve))
        print(t["reply"]); print(json.dumps({"retrieved": t["retrieved"], "stored": t["stored"], "money": t["money"]}, default=str, indent=2))
    else:
        sess = Session(cid(a.who))
        print(f"Chatting as {a.who}. Blank line starts a new session; Ctrl-D quits.")
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                break
            if not text:
                sess = Session(cid(a.who)); print("(new session)"); continue
            t = sess.send(text, approve=_approver(None))
            print(t["reply"])
            if t["stored"].get("stored") or t["stored"].get("rejected"):
                print(f"  [memory] stored={[x['content'] for x in t['stored']['stored']]} rejected={[(x['content'], x['reason']) for x in t['stored']['rejected']]}")


if __name__ == "__main__":
    main()
