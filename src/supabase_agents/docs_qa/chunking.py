"""Parse help-center markdown, sanitize text addressed to an AI, and chunk by `## ` heading."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HELP_CENTER = Path(__file__).parent / "help_center"

# Sentences that address an AI/assistant or try to override instructions. Removed at ingestion and the chunk
# is flagged. Defense in depth: the answer prompt also treats chunk text as data.
_INJECTION = re.compile(
    r"(ignore (all |any |your )?(previous |prior )?instructions|note to (ai|llm|assistant)s?|"
    r"^\s*(ai|assistant|llm|chatbot)\s*:|(ai|llm) assistants?\s*:|system prompt)",
    re.I,
)


@dataclass
class Article:
    id: str
    title: str
    audience: str
    status: str
    superseded_by: list[str]
    body: str


@dataclass
class Chunk:
    id: str
    article_id: str
    heading: str
    content: str
    audience: str
    flagged: bool = False
    removed: list[str] = field(default_factory=list)

    def embed_text(self, title: str) -> str:
        return f"{title}. {self.heading}. {self.content}"


def parse_article(text: str) -> Article:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError("missing front matter")
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    sup = [s.strip() for s in meta.get("superseded_by", "").split(",") if s.strip()]
    return Article(meta["id"], meta["title"], meta["audience"], meta["status"], sup, m.group(2).strip())


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def sanitize(text: str) -> tuple[str, list[str]]:
    """Drop sentences that address an AI. Returns (clean_text, removed_sentences)."""
    keep, removed = [], []
    for s in split_sentences(text):
        (removed if _INJECTION.search(s) else keep).append(s)
    return " ".join(keep), removed


def chunk_article(a: Article) -> list[Chunk]:
    chunks: list[Chunk] = []
    parts = re.split(r"^## +", a.body, flags=re.M)
    sections = [p for p in parts if p.strip()]
    for n, sec in enumerate(sections, 1):
        heading, _, text = sec.partition("\n")
        text = " ".join(text.split())
        clean, removed = sanitize(text)
        chunks.append(Chunk(f"{a.id}#{n}", a.id, heading.strip(), clean, a.audience, bool(removed), removed))
    return chunks


def load_help_center(folder: Path = HELP_CENTER) -> list[Article]:
    return [parse_article(p.read_text()) for p in sorted(folder.glob("*.md"))]
