"""Generate the migration SQL for a target schema. One SQL text is used for the trial schema and, after
approval, for `migration`, so what was proven is exactly what gets applied. `{S}` is the schema placeholder."""

from __future__ import annotations

import json
from datetime import datetime

from .infer import singular


def lit(v, typ: str | None = None) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat()}'::timestamptz"
    if typ == "jsonb" or isinstance(v, (dict,)):
        return "'" + json.dumps(v, sort_keys=True).replace("'", "''") + "'::jsonb"
    if isinstance(v, list):
        base = (typ or "text[]")
        return "array[" + ", ".join(lit(x) for x in v) + f"]::{base}" if v else f"'{{}}'::{base}"
    s = "'" + str(v).replace("'", "''") + "'"
    return f"{s}::uuid" if typ == "uuid" else s


def ddl(schema: dict) -> list[str]:
    tables = schema["tables"]
    out = ["create schema if not exists {S};"]
    for ck in schema["order"]:
        t = tables[ck]
        parts = []
        for c in t["columns"]:
            line = f"  {c['name']} {c['type']}"
            if c.get("pk"):
                line += " primary key"
            elif not c["nullable"]:
                line += " not null"
            if c.get("unique"):
                line += " unique"
            if c.get("fk"):
                target = tables[c["fk"]["coll"]]["name"]
                line += f" references {{S}}.{target}({c['fk']['col']})"
            parts.append(line)
        out.append(f"create table {{S}}.{t['name']} (\n" + ",\n".join(parts) + "\n);")
    for ck in schema["order"]:
        t = tables[ck]
        for j in t["join"]:
            owner, target = t["name"], tables[j["target"]]["name"]
            oc, tc = f"{singular(owner)}_path", f"{singular(target)}_path"
            out.append(f"create table {{S}}.{j['name']} (\n  {oc} text not null references {{S}}.{owner}(_path) on delete cascade,\n"
                       f"  {tc} text not null references {{S}}.{target}(_path),\n  position int not null,\n  primary key ({oc}, position)\n);")
    # indexes on every foreign key (the advisor's unindexed_foreign_keys check)
    for ck in schema["order"]:
        t = tables[ck]
        for c in t["columns"]:
            if c.get("fk") or c.get("kind") == "uid":
                out.append(f"create index on {{S}}.{t['name']} ({c['name']});")
        for j in t["join"]:
            target = tables[j["target"]]["name"]
            out.append(f"create index on {{S}}.{j['name']} ({singular(target)}_path);")
    return out


def all_table_names(schema: dict) -> list[str]:
    names = []
    for ck in schema["order"]:
        t = schema["tables"][ck]
        names.append(t["name"])
        names += [j["name"] for j in t["join"]]
    return names


def security(schema: dict, translation: dict) -> list[str]:
    tables = schema["tables"]
    out = ["grant usage on schema {S} to authenticated, anon;"]
    for name in all_table_names(schema):
        out.append(f"alter table {{S}}.{name} enable row level security;")
        out.append(f"grant select, insert, update, delete on {{S}}.{name} to authenticated;")
        out.append(f"grant select on {{S}}.{name} to anon;")
    for h in translation["helpers"].values():
        out.append(h)
    out.append("grant execute on all functions in schema {S} to authenticated, anon;")
    for p in translation["policies"]:
        if not p["deny"]:
            out.append(p["sql"])
    # join tables have no Firestore rules of their own: readable when the parent row is readable (its RLS applies)
    for ck in schema["order"]:
        t = tables[ck]
        for j in t["join"]:
            oc = f"{singular(t['name'])}_path"
            out.append(f"create policy fb_{j['name']}_select on {{S}}.{j['name']} for select to authenticated, anon "
                       f"using (exists (select 1 from {{S}}.{t['name']} p where p._path = {oc}));")
    return out


def data(schema: dict, converted: dict) -> list[str]:
    tables = schema["tables"]
    out = []
    for ck in schema["order"]:
        t = tables[ck]
        rows = converted["rows"].get(ck, [])
        if not rows:
            continue
        cols = [c["name"] for c in t["columns"]]
        types = {c["name"]: c["type"] for c in t["columns"]}
        values = ",\n".join("  (" + ", ".join(lit(r.get(c), types[c]) for c in cols) + ")" for r in rows)
        out.append(f"insert into {{S}}.{t['name']} ({', '.join(cols)}) values\n{values};")
    for ck in schema["order"]:
        t = tables[ck]
        for j in t["join"]:
            rows = converted["join_rows"].get(j["name"], [])
            if not rows:
                continue
            target = tables[j["target"]]["name"]
            oc, tc = f"{singular(t['name'])}_path", f"{singular(target)}_path"
            values = ",\n".join(f"  ({lit(r['owner_path'])}, {lit(r['target_path'])}, {r['position']})" for r in rows)
            out.append(f"insert into {{S}}.{j['name']} ({oc}, {tc}, position) values\n{values};")
    return out


def migration_sql(schema: dict, translation: dict, converted: dict, header: str = "") -> str:
    parts = [header] if header else []
    parts += ["-- schema"] + ddl(schema) + ["-- data"] + data(schema, converted) + ["-- row-level security (translated from firestore.rules)"] + security(schema, translation)
    for f in translation["flags"]:
        parts.append(f"-- FLAGGED, no policy (fails closed): {f['rule']} (line {f['line']}): {f['reason']}")
    return "\n\n".join(parts) + "\n"


def render(sql: str, schema_name: str) -> str:
    return sql.replace("{S}", schema_name)
