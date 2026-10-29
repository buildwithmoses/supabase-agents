"""Read a Firestore export into flat document records.

Each record: {"coll": "clubs/members" (collection key, ids stripped), "path": "clubs/club_oak/members/u1",
"id": "u1", "parents": [("clubs", "club_oak")], "fields": {...}}. Subcollections are walked recursively.
"""

from __future__ import annotations

import json
from pathlib import Path


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def walk(export: dict) -> list[dict]:
    out: list[dict] = []

    def rec(coll_name: str, docs: dict, prefix: str, key_prefix: str, parents: list):
        for doc_id in sorted(docs):
            doc = docs[doc_id]
            path = f"{prefix}{coll_name}/{doc_id}"
            out.append({"coll": f"{key_prefix}{coll_name}", "path": path, "id": doc_id,
                        "parents": list(parents), "fields": doc.get("fields", {})})
            for sub, subdocs in (doc.get("__collections__") or {}).items():
                rec(sub, subdocs, path + "/", f"{key_prefix}{coll_name}/", parents + [(coll_name, doc_id)])

    for coll in sorted(export):
        rec(coll, export[coll], "", "", [])
    return out


def coll_key_of_path(path: str) -> str:
    """'clubs/club_oak/members/u1' -> 'clubs/members' (the collection key of the document)."""
    parts = path.strip("/").split("/")
    return "/".join(parts[0::2])


def is_ts(v) -> bool:
    return isinstance(v, dict) and v.get("__type__") == "timestamp"


def is_ref(v) -> bool:
    return isinstance(v, dict) and v.get("__type__") == "ref"
