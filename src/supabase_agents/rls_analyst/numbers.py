"""Number check: every number in the answer must come from the query result.

Supported if it equals a result cell (or a number inside a text cell such as a date), allowing the precision it
is written at ("$7,546.34", "7,546", "7.5k", "about 7,500" within 5%); a ratio cell shown as a percent; a cents
cell shown in dollars (cents / 100); the row count; or a number the user typed or that appears in the SQL.
"""

from __future__ import annotations

import math
import re

NUM_RE = re.compile(r"(?<![\w.])-?\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\s?(?:%|percent\b|[kK]\b|million\b))?")
DATE_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?|\d{1,2}:\d{2}(?:\s?[ap]m)?)\b", re.I)
MONTHS = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b", re.I)


def _cells(rows: list[list]) -> set[float]:
    out: set[float] = set()
    for r in rows:
        for v in r:
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and math.isfinite(v):
                out.add(float(v))
            elif isinstance(v, str):
                for m in re.finditer(r"\d+(?:\.\d+)?", v.replace(",", "")):
                    out.add(float(m.group()))
    return out


def _close(x: float, y: float, decimals: int, integer_written: bool) -> bool:
    if abs(x - y) <= 0.5 * 10 ** (-decimals) + 1e-9:
        return True
    if integer_written and y != 0 and abs(x - y) / abs(y) <= 0.05 and x % 10 == 0:
        return True  # "about 7,500" for 7,546
    return False


def check(answer: str, rows: list[list], extra_text: str = "") -> list[str]:
    text = MONTHS.sub(" ", DATE_RE.sub(" ", answer or ""))
    cells = _cells(rows)
    allowed = cells | {c / 100 for c in cells} | {c * 100 for c in cells if abs(c) <= 1} | {float(len(rows))}
    allowed |= {float(m.group().replace(",", "")) for m in re.finditer(r"\d+(?:\.\d+)?", extra_text.replace(",", ""))}
    bad = []
    for m in NUM_RE.finditer(text):
        raw, suf = m.group(1).replace(",", ""), (m.group(2) or "").strip().lower()
        val = float(raw)
        dec = len(raw.split(".")[1]) if "." in raw else 0
        if suf == "k":
            val *= 1000; dec = 0
        elif suf == "million":
            val *= 1e6; dec = 0
        if 1900 <= val <= 2100 and dec == 0:
            continue  # years
        cands = allowed | ({v * 100 for v in cells} if suf in ("%", "percent") else set())
        if not any(_close(val, c, dec, dec == 0) for c in cands):
            bad.append(m.group(0).strip())
    return bad
