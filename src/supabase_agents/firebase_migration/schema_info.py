"""The view of the inferred schema that rule translation and the proof need."""

from __future__ import annotations

from .infer import singular


def policy_info(schema: dict) -> dict:
    info = {}
    for ck, t in schema["tables"].items():
        cols, uid_cols, types = {}, {}, {}
        for c in t["columns"]:
            types[c["name"]] = c["type"]
            src = c.get("source")
            if src and c.get("kind") not in ("flatten",):
                cols[src] = c["name"]
                if c.get("kind") == "uid":
                    uid_cols[src] = True
        parent = t.get("parent_coll")
        info[ck] = {"table": t["name"], "columns": cols, "uid_cols": uid_cols, "types": types,
                    "parent_col": f"{singular(parent)}_id" if parent else None, "id_uid_col": t.get("id_is_uid_column"),
                    "coll_name": ck.split("/")[-1]}
    return info
