"""Apply the docs_qa migration, then (re)load the help center: chunk, sanitize, embed locally, upsert.

    uv run python -m supabase_agents.docs_qa.ingest
Idempotent. Only touches the docs_qa schema.
"""

from __future__ import annotations

from pathlib import Path

from ..core.config import PROJECT_ROOT, embed
from ..core.db import connect, vector_literal
from . import STAFF_ID
from .chunking import chunk_article, load_help_center

MIGRATION = PROJECT_ROOT / "supabase" / "migrations" / "20261029130000_docs_qa_schema.sql"


def ingest(folder: Path | None = None) -> dict:
    articles = load_help_center(folder) if folder else load_help_center()
    chunks = [(a, c) for a in articles for c in chunk_article(a)]
    vectors = embed([c.embed_text(a.title) for a, c in chunks])
    with connect() as conn:
        conn.execute(MIGRATION.read_text())
        conn.execute("delete from docs_qa.chunks")
        conn.execute("delete from docs_qa.articles")
        for a in articles:
            conn.execute("insert into docs_qa.articles (id, title, audience, status, superseded_by) values (%s,%s,%s,%s,%s)",
                         (a.id, a.title, a.audience, a.status, a.superseded_by))
        for (a, c), v in zip(chunks, vectors):
            conn.execute("insert into docs_qa.chunks (id, article_id, heading, content, audience, flagged, embedding) "
                         "values (%s,%s,%s,%s,%s,%s,%s::extensions.vector)",
                         (c.id, c.article_id, c.heading, c.content, c.audience, c.flagged, vector_literal(v)))
        conn.execute("insert into docs_qa.staff_members (user_id) values (%s) on conflict do nothing", (STAFF_ID,))
        conn.commit()
    flagged = [(c.id, c.removed) for _, c in chunks if c.flagged]
    return {"articles": len(articles), "chunks": len(chunks), "staff_only_chunks": sum(c.audience == "staff" for _, c in chunks),
            "deprecated_articles": sum(a.status == "deprecated" for a in articles), "flagged": flagged}


if __name__ == "__main__":
    import json
    print(json.dumps(ingest(), indent=2))
