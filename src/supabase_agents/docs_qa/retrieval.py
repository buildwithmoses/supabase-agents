"""Retrieval.

baseline: docs_qa.hybrid_search() (full text + pgvector, reciprocal rank fusion, deprecated last), run INSIDE a
          read-only transaction as the asking user (role authenticated + JWT claims), so RLS decides what a
          player or staff member can see. Then code drops any deprecated chunk whose replacement article was
          also retrieved.
          Finally, the other sections of the top articles are added after their best section (help-center
          articles are short, and an answer often spans two headings), still inside the user's transaction.
naive:    the regression. Vector-only similarity as the postgres superuser (bypasses RLS), no deprecation rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.config import embed
from ..core.db import connect, user_tx, vector_literal

K = 8


@dataclass
class Hit:
    id: str
    article_id: str
    title: str
    heading: str
    content: str
    deprecated: bool
    superseded_by: list[str]
    audience: str
    score: float


def rrf_merge(*rankings: list[str], k: int = 50) -> list[tuple[str, float]]:
    """Reciprocal rank fusion, the same formula the SQL uses (1-based ranks). Used in tests to check the SQL."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc in enumerate(ranking, 1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def drop_superseded(hits: list[Hit]) -> list[Hit]:
    present = {h.article_id for h in hits if not h.deprecated}
    return [h for h in hits if not (h.deprecated and set(h.superseded_by) & present)]


def expand_siblings(hits: list[Hit], siblings: list[Hit], max_articles: int = 6) -> list[Hit]:
    """Put each top article's other sections right after its best-ranked section. Pure: tested offline."""
    top = []
    for h in hits:
        if h.article_id not in top:
            top.append(h.article_id)
    top = top[:max_articles]
    seen = {h.id for h in hits}
    by_article: dict[str, list[Hit]] = {}
    for s in siblings:
        if s.article_id in top and s.id not in seen:
            by_article.setdefault(s.article_id, []).append(s)
    out, done = [], set()
    for h in hits:
        out.append(h)
        if h.article_id in by_article and h.article_id not in done:
            out.extend(sorted(by_article[h.article_id], key=lambda x: x.id))
            done.add(h.article_id)
    return out


def search(question: str, user_id: str, *, k: int = K, variant: str = "baseline") -> list[Hit]:
    qv = vector_literal(embed([question])[0])
    if variant == "naive":
        with connect() as conn:
            rows = conn.execute(
                "select c.id, c.article_id, a.title, c.heading, c.content, a.status = 'deprecated', a.superseded_by, c.audience, "
                "1 - (c.embedding operator(extensions.<=>) %s::extensions.vector) as score "
                "from docs_qa.chunks c join docs_qa.articles a on a.id = c.article_id "
                "order by c.embedding operator(extensions.<=>) %s::extensions.vector limit %s", (qv, qv, k)).fetchall()
        return [Hit(*r) for r in rows]
    with user_tx(user_id) as conn:
        rows = conn.execute("select * from docs_qa.hybrid_search(%s, %s::extensions.vector, %s)", (question, qv, k)).fetchall()
        hits = drop_superseded([Hit(*r) for r in rows])
        arts = list(dict.fromkeys(h.article_id for h in hits))[:6]
        sib = conn.execute(
            "select c.id, c.article_id, a.title, c.heading, c.content, a.status = 'deprecated', a.superseded_by, c.audience, 0.0 "
            "from docs_qa.chunks c join docs_qa.articles a on a.id = c.article_id where c.article_id = any(%s)", (arts,)).fetchall()
    return expand_siblings(hits, [Hit(*r) for r in sib])
