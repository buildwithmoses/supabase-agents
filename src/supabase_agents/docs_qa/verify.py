"""Code checks on a drafted answer. Pure functions: no DB, no model.

verify(draft, retrieved, question) -> list of problems (empty = OK):
  - an answer (not "don't know") must cite at least one chunk
  - every cited chunk id must have been retrieved for THIS question
  - every number in the answer (prices, counts, times, percentages) must appear in a cited chunk
    (numbers the user wrote in the question are allowed)
  - every "quoted string" in the answer must appear word for word in a cited chunk
"""

from __future__ import annotations

import re

_NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?(?::\d{2})?%?")


def numbers(text: str) -> set[str]:
    """Normalized numbers: '$1,500.00' -> '1500', '4%' -> '4', '2:00' -> '2:00', '10,000' -> '10000'."""
    out = set()
    for m in _NUM.findall(text):
        n = m.lstrip("$").rstrip("%").replace(",", "")
        if ":" not in n and "." in n:
            n = n.rstrip("0").rstrip(".") if re.fullmatch(r"\d+\.\d+", n) else n
        out.add(n)
    return out


def quotes(text: str) -> list[str]:
    return [q.strip() for q in re.findall(r"[\"“]([^\"”]{3,})[\"”]", text)]


def verify(draft: dict, retrieved: dict[str, str], question: str = "") -> list[str]:
    if draft.get("dont_know"):
        return []
    answer = draft.get("answer", "") or ""
    cites = [c for c in draft.get("citations") or []]
    problems = []
    if not answer.strip():
        problems.append("empty answer")
    if not cites:
        problems.append("answer has no citations")
    bad = [c for c in cites if c not in retrieved]
    if bad:
        problems.append(f"cited chunks that were not retrieved for this question: {bad}")
    cited_text = " ".join(retrieved[c] for c in cites if c in retrieved)
    have = numbers(cited_text) | numbers(question)
    missing = sorted(numbers(answer) - have)
    if missing:
        problems.append(f"numbers not found in the cited chunks: {missing}")
    low = cited_text.lower()
    for q in quotes(answer):
        if q.lower() not in low:
            problems.append(f'quote not found in the cited chunks: "{q}"')
    return problems
