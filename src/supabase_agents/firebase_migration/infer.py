"""Infer a relational schema from Firestore documents. All decisions are code; the model only explains
and proposes names (graph.review). Every conversion that changes a value is logged as a fix, and every
document that can't be loaded is logged as a skip, with a reason.

Rules (documented in docs/firebase-migration.md):
- Each collection -> a table. Columns `_path` (PK, the full document path) and `id` (document id).
  A subcollection gets `<parent>_id` referencing the top-level parent table's `id`.
- Timestamps -> timestamptz (ISO strings in a timestamp field are parsed and logged as fixes).
- References -> `<field>_path text` FK to the target table's `_path`. Required if present in every doc.
- Arrays of references -> a join table `<table>_<field>` (owner path, target path, position).
- Arrays of scalars -> a Postgres array. Other arrays -> jsonb.
- Maps whose keys are the same in >= 90% of docs and whose values are scalars -> flattened `<field>_<key>`.
  Other maps -> jsonb.
- Numbers: mostly Firestore integers (all integral) -> bigint, else double precision. Numeric strings are converted (fix); non-numeric
  strings in a numeric field become NULL (fix).
- Firebase user ids (`uid`, `*_uid`) -> uuid columns (`user_id`, `*_id`) via a deterministic uuid5 mapping,
  so auth.uid() can be compared to them in RLS.
- `<name>_id` text fields whose values are all ids of a top-level collection `<name>s` -> FK to that table.
- Nullable unless the field is present and non-null in every document.
- Exact duplicate documents (same parent and same fields) -> later ones skipped. Docs whose required reference points
  nowhere -> skipped. Optional references that point nowhere -> NULL (fix). Missing array elements -> dropped (fix).
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .export import coll_key_of_path, is_ref, is_ts

UID_NS = uuid.UUID("6f1b8f0e-9a7c-4c2e-9d1e-5b2f8f0a3c11")


def uid_to_uuid(uid: str) -> str:
    return str(uuid.uuid5(UID_NS, f"firebase:{uid}"))


def singular(name: str) -> str:
    return name[:-1] if name.endswith("s") else name


def is_uid_field(name: str) -> bool:
    return name == "uid" or name.endswith("_uid")


def uid_column(name: str) -> str:
    return "user_id" if name == "uid" else name[:-4] + "_id"


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v) if re.fullmatch(r"-?\d+(\.\d+)?", v.strip()) else None
        except ValueError:
            return None
    return None


def parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def ts_to_dt(v: dict) -> datetime:
    return datetime.fromtimestamp(v["seconds"] + v.get("nanos", 0) / 1e9, tz=timezone.utc)


def _scalar(v) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _kind(values: list) -> tuple[str, dict]:
    """Decide a field kind from its present values. Returns (kind, info)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return "text", {}
    if all(is_ts(v) or (isinstance(v, str) and parse_iso(v)) for v in vals) and any(is_ts(v) for v in vals):
        return "timestamptz", {}
    if all(is_ref(v) for v in vals):
        targets = {coll_key_of_path(v["path"]) for v in vals}
        return ("ref", {"target": targets.pop()}) if len(targets) == 1 else ("jsonb", {"why": "references to several collections"})
    if all(isinstance(v, list) for v in vals):
        elems = [e for v in vals for e in v]
        if elems and all(is_ref(e) for e in elems):
            targets = {coll_key_of_path(e["path"]) for e in elems}
            if len(targets) == 1:
                return "ref_array", {"target": targets.pop()}
        if all(isinstance(e, str) for e in elems):
            return "text[]", {}
        if all(isinstance(e, (int, float)) and not isinstance(e, bool) for e in elems):
            return "double precision[]", {}
        return "jsonb", {"why": "array of mixed values"}
    if all(isinstance(v, dict) and not is_ts(v) and not is_ref(v) for v in vals):
        keysets = Counter(tuple(sorted(v)) for v in vals)
        top, n = keysets.most_common(1)[0]
        if n / len(vals) >= 0.9 and all(_scalar(x) for v in vals for x in v.values()):
            sub = {k: _kind([v.get(k) for v in vals])[0] for k in top}
            return "flatten", {"keys": list(top), "sub": sub}
        return "jsonb", {"why": "free-form map: key sets differ across documents" if len(keysets) > 1 else "map with nested values"}
    if all(isinstance(v, bool) for v in vals):
        return "boolean", {}
    nums = [_num(v) for v in vals]
    n_numeric_native = sum(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
    if n_numeric_native and n_numeric_native >= len(vals) / 2:
        ok = [x for x in nums if x is not None]
        n_int = sum(isinstance(v, int) and not isinstance(v, bool) for v in vals)
        # Firestore keeps integers and doubles apart: integers (with a stray integral double) -> bigint
        integral = n_int > len(vals) / 2 and all(float(x).is_integer() for x in ok)
        return ("bigint" if integral else "double precision"), {}
    if all(isinstance(v, str) for v in vals):
        return "text", {}
    return "jsonb", {"why": "mixed types"}


def infer(docs: list[dict]) -> dict:
    """Return the schema: {"tables": {coll_key: table}, "order": [...], "ambiguities": [...]}"""
    by_coll: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        by_coll[d["coll"]].append(d)
    top_ids = {k: {d["id"] for d in v} for k, v in by_coll.items() if "/" not in k}
    names_used: Counter = Counter(k.split("/")[-1] for k in by_coll)
    tables: dict[str, dict] = {}
    amb: list[dict] = []
    for ck, cdocs in by_coll.items():
        last = ck.split("/")[-1]
        tname = last if names_used[last] == 1 else ck.replace("/", "_")
        cols: list[dict] = [{"name": "_path", "type": "text", "pk": True, "nullable": False, "source": None},
                            {"name": "id", "type": "text", "nullable": False, "source": None, "unique": "/" not in ck}]
        parents = ck.split("/")[:-1]
        if len(parents) > 1:
            amb.append({"item": f"{ck}", "kind": "nesting", "detail": "nested more than one level: parent keys use the full parent path only"})
        if parents:
            p = parents[-1]
            cols.append({"name": f"{singular(p)}_id", "type": "text", "nullable": False, "source": None,
                         "fk": {"coll": p, "col": "id"}, "parent": True})
        join_tables = []
        field_names = sorted({f for d in cdocs for f in d["fields"]})
        for f in field_names:
            values = [d["fields"].get(f) for d in cdocs]
            present = [v for v in values if v is not None]
            required = len(present) == len(cdocs)
            kind, info = _kind(values)
            if kind == "ref":
                cols.append({"name": f"{f}_path", "type": "text", "nullable": not required, "source": f, "kind": "ref",
                             "fk": {"coll": info["target"], "col": "_path"}})
            elif kind == "ref_array":
                join_tables.append({"field": f, "target": info["target"]})
            elif kind == "flatten":
                amb.append({"item": f"{tname}.{f}", "kind": "flatten", "detail": f"map with the same keys {info['keys']} in >=90% of docs: flattened into columns"})
                for k in info["keys"]:
                    cols.append({"name": f"{f}_{k}", "type": info["sub"][k] if info["sub"][k] != "flatten" else "jsonb",
                                 "nullable": True, "source": f, "subkey": k, "kind": "flatten"})
            elif is_uid_field(f) and kind == "text":
                cols.append({"name": uid_column(f), "type": "uuid", "nullable": not required, "source": f, "kind": "uid"})
                amb.append({"item": f"{tname}.{f}", "kind": "uid", "detail": f"Firebase user id mapped to uuid column {uid_column(f)} (uuid5 of the Firebase uid) so RLS can compare it to auth.uid()"})
            else:
                col = {"name": f, "type": kind if kind != "flatten" else "jsonb", "nullable": not required, "source": f, "kind": kind}
                if kind == "jsonb":
                    amb.append({"item": f"{tname}.{f}", "kind": "jsonb", "detail": info.get("why", "free-form")})
                if kind in ("bigint", "double precision") and any(isinstance(v, str) for v in present):
                    strs = [v for v in present if isinstance(v, str)]
                    n_null = sum(_num(v) is None for v in strs)
                    amb.append({"item": f"{tname}.{f}", "kind": "mixed_numeric",
                                "detail": f"numbers mixed with strings: {len(strs) - n_null} numeric string(s) converted"
                                          + (f", {n_null} non-numeric value(s) set to NULL" if n_null else "")})
                    col["nullable"] = True if any(_num(v) is None for v in present) else col["nullable"]
                if kind == "timestamptz" and any(isinstance(v, str) for v in present):
                    amb.append({"item": f"{tname}.{f}", "kind": "mixed_timestamp", "detail": "Firestore timestamps mixed with ISO strings: strings parsed"})
                if kind == "text" and f.endswith("_id"):
                    target = f[:-3] + "s"
                    if target in top_ids:
                        if all(v in top_ids[target] for v in present):
                            col["fk"] = {"coll": target, "col": "id"}
                            amb.append({"item": f"{tname}.{f}", "kind": "name_fk", "detail": f"every value is an id in {target}: added a foreign key"})
                        else:
                            amb.append({"item": f"{tname}.{f}", "kind": "name_fk_skipped", "detail": f"looks like a {target} id but some values don't exist: no foreign key"})
                cols.append(col)
            if not required and kind not in ("ref_array",):
                amb.append({"item": f"{tname}.{f}", "kind": "nullable", "detail": f"missing or null in {len(cdocs) - len(present)} of {len(cdocs)} docs: nullable"})
        # doc id equals a uid field in every doc? then lookups by the doc id can use that uuid column
        id_alias = None
        for f in field_names:
            if is_uid_field(f) and all(d["fields"].get(f) == d["id"] for d in cdocs):
                id_alias = uid_column(f)
        tables[ck] = {"coll": ck, "name": tname, "columns": cols, "join": join_tables, "id_is_uid_column": id_alias,
                      "parent_coll": parents[-1] if parents else None}
    for ck, t in tables.items():
        for j in t["join"]:
            j["name"] = f"{t['name']}_{j['field']}"
            amb.append({"item": f"{t['name']}.{j['field']}", "kind": "join_table", "detail": f"array of references -> join table {j['name']}"})
    # dependency order: parents and FK targets first
    order: list[str] = []
    def visit(ck, seen=()):
        if ck in order or ck in seen:
            return
        for c in tables[ck]["columns"]:
            if c.get("fk") and c["fk"]["coll"] in tables and c["fk"]["coll"] != ck:
                visit(c["fk"]["coll"], seen + (ck,))
        order.append(ck)
    for ck in sorted(tables):
        visit(ck)
    return {"tables": tables, "order": order, "ambiguities": amb}


def convert_rows(docs: list[dict], schema: dict, known_paths: dict | None = None) -> dict:
    """Convert docs to rows. Returns {"rows": {coll: [row]}, "join_rows": {jt: [row]}, "skips": [...], "fixes": [...]}."""
    tables = schema["tables"]
    paths_by_coll = defaultdict(set)
    for k, v in (known_paths or {}).items():
        paths_by_coll[k] |= set(v)
    for d in docs:
        paths_by_coll[d["coll"]].add(d["path"])
    rows: dict[str, list[dict]] = defaultdict(list)
    join_rows: dict[str, list[dict]] = defaultdict(list)
    skips: list[dict] = []
    fixes: list[dict] = []
    seen: dict[str, dict] = {}
    # duplicates first, so a skipped duplicate never counts as a reference target
    for d in docs:
        key = d["coll"] + "|" + json.dumps(d["parents"]) + "|" + json.dumps(d["fields"], sort_keys=True)
        if key in seen:
            skips.append({"path": d["path"], "reason": "duplicate", "detail": f"identical to {seen[key]['path']}"})
            paths_by_coll[d["coll"]].discard(d["path"])
            d["_skip"] = True
        else:
            seen[key] = d
    for d in docs:
        if d.get("_skip"):
            continue
        t = tables[d["coll"]]
        row: dict = {"_path": d["path"], "id": d["id"]}
        if d["parents"]:
            row[t["columns"][2]["name"]] = d["parents"][-1][1]
        bad = None
        for c in t["columns"]:
            src = c.get("source")
            if not src:
                continue
            v = d["fields"].get(src)
            kind = c.get("kind")
            where = f"{d['path']}.{src}"
            if kind == "flatten":
                sv = (v or {}).get(c["subkey"]) if isinstance(v, dict) else None
                row[c["name"]] = sv
                continue
            if v is None:
                row[c["name"]] = None
                continue
            if kind == "ref":
                if v["path"] not in paths_by_coll[c["fk"]["coll"]]:
                    if not c["nullable"]:
                        bad = {"path": d["path"], "reason": "orphan_ref", "detail": f"{src} -> {v['path']} does not exist"}
                        break
                    fixes.append({"where": where, "fix": "orphan reference set to NULL", "from": v["path"], "to": None})
                    row[c["name"]] = None
                else:
                    row[c["name"]] = v["path"]
            elif kind == "uid":
                row[c["name"]] = uid_to_uuid(v)
            elif c["type"] == "timestamptz":
                if is_ts(v):
                    row[c["name"]] = ts_to_dt(v)
                else:
                    dt = parse_iso(v) if isinstance(v, str) else None
                    fixes.append({"where": where, "fix": "ISO string parsed as timestamp" if dt else "unparseable timestamp set to NULL", "from": v, "to": dt.isoformat() if dt else None})
                    row[c["name"]] = dt
            elif c["type"] in ("bigint", "double precision"):
                n = _num(v)
                if isinstance(v, str):
                    fixes.append({"where": where, "fix": "numeric string converted" if n is not None else "non-numeric value set to NULL", "from": v, "to": n})
                elif c["type"] == "bigint" and isinstance(v, float):
                    fixes.append({"where": where, "fix": "integral float stored as integer", "from": v, "to": int(v)})
                row[c["name"]] = (int(n) if c["type"] == "bigint" and n is not None else n)
            elif c["type"] == "jsonb":
                row[c["name"]] = v
            else:
                row[c["name"]] = v
        if bad:
            skips.append(bad)
            paths_by_coll[d["coll"]].discard(d["path"])
            continue
        rows[d["coll"]].append(row)
        for j in t["join"]:
            arr = d["fields"].get(j["field"]) or []
            pos = 0
            for e in arr:
                if e["path"] not in paths_by_coll[j["target"]]:
                    fixes.append({"where": f"{d['path']}.{j['field']}", "fix": "reference to a missing document dropped from the array", "from": e["path"], "to": None})
                    continue
                join_rows[j["name"]].append({"owner_path": d["path"], "target_path": e["path"], "position": pos})
                pos += 1
    # a row skipped late can orphan join rows that point at it
    alive = {r["_path"] for rs in rows.values() for r in rs}
    for jt, jr in join_rows.items():
        kept = [r for r in jr if r["owner_path"] in alive and r["target_path"] in alive]
        join_rows[jt] = kept
    return {"rows": dict(rows), "join_rows": dict(join_rows), "skips": skips, "fixes": fixes}
