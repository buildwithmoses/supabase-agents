"""Parse Firestore security rules (the subset real apps use) and translate them to Postgres RLS.

Parsing: `match` blocks (nested), `function` definitions, and `allow <ops>: if <expr>;` statements.
Expressions: || && ! == != < > <= >= in, member access, calls, lists, strings, numbers, booleans, null,
and document paths like /databases/$(database)/documents/clubs/$(clubId)/members/$(request.auth.uid).

Translation (the documented pattern table; anything else is FLAGGED for a human and gets no policy, so the
table fails closed):

| Firestore rule pattern                                  | RLS                                                   |
|---------------------------------------------------------|-------------------------------------------------------|
| request.auth != null / == null                          | auth.uid() is not null / is null                      |
| request.auth.uid == resource.data.<uid field>           | auth.uid() = <uuid column>   (USING)                  |
| request.auth.uid == request.resource.data.<uid field>   | auth.uid() = <uuid column>   (WITH CHECK)             |
| resource.data.<f> / request.resource.data.<f>           | the column for <f> (old row in USING, new row in CHECK)|
| {pathVar} of the match                                  | id, or the parent key column (club_id)                |
| exists(<doc path>)                                      | security-definer helper fb_exists_<table>(...)        |
| get(<doc path>).data.<f>                                | security-definer helper fb_get_<table>_<f>(...)       |
| x in ['a','b']                                          | x = any(array['a','b'])                               |
| user-defined functions                                  | inlined with their arguments                          |
| true / false                                            | policy with true / no policy (deny)                   |
| on create, resource.data is null                        | that comparison is false (Firestore errors -> deny)   |

Flagged, never guessed: request.auth.token.* (custom claims), request.time, timestamp.*, duration.*,
.keys()/.size()/.hasOnly()/.hasAll() and other method calls, arithmetic, and rules that compare old and new
data in one update. Helpers are SECURITY DEFINER so a policy on `members` can look at `members` without
recursion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------- tokenizer / parser

TOKEN = re.compile(r"""\s*(?:(//[^\n]*)|(\d+(?:\.\d+)?)|('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(==|!=|<=|>=|&&|\|\||[()\[\]{}.,;:!<>=])|([A-Za-z_$][A-Za-z0-9_$]*))""")


class ParseError(Exception):
    pass


def tokenize(src: str) -> list[tuple[str, str]]:
    toks, i = [], 0
    while i < len(src):
        if src[i].isspace():
            i += 1; continue
        if src[i] == "/" and src[i:i + 2] != "//":
            # a document path literal: read until whitespace or a closing paren/comma at depth 0
            j, depth = i, 0
            while j < len(src):
                ch = src[j]
                if src[j:j + 2] == "$(":
                    depth += 1; j += 2; continue
                if ch == "(" and depth:
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif (ch.isspace() or ch in ",;{") and depth == 0:
                    break
                j += 1
            toks.append(("path", src[i:j])); i = j; continue
        m = TOKEN.match(src, i)
        if not m or m.end() == i:
            raise ParseError(f"unexpected character {src[i]!r} at {i}")
        if m.group(1):
            i = m.end(); continue
        if m.group(2):
            toks.append(("num", m.group(2)))
        elif m.group(3):
            toks.append(("str", m.group(3)[1:-1]))
        elif m.group(4):
            toks.append(("op", m.group(4)))
        else:
            toks.append(("id", m.group(5)))
        i = m.end()
    return toks


class Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else ("eof", "")

    def take(self, val=None):
        tok = self.peek()
        if val is not None and tok[1] != val:
            raise ParseError(f"expected {val!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def expr(self):
        node = self.and_()
        while self.peek()[1] == "||":
            self.take(); node = ("bin", "||", node, self.and_())
        return node

    def and_(self):
        node = self.unary()
        while self.peek()[1] == "&&":
            self.take(); node = ("bin", "&&", node, self.unary())
        return node

    def unary(self):
        if self.peek()[1] == "!":
            self.take(); return ("not", self.unary())
        return self.cmp()

    def cmp(self):
        node = self.postfix()
        if self.peek()[1] in ("==", "!=", "<", ">", "<=", ">=", "in"):
            op = self.take()[1]
            node = ("bin", op, node, self.postfix())
        return node

    def postfix(self):
        node = self.primary()
        while True:
            if self.peek()[1] == ".":
                self.take(); node = ("attr", node, self.take()[1])
            elif self.peek()[1] == "(":
                self.take(); args = []
                while self.peek()[1] != ")":
                    args.append(self.expr())
                    if self.peek()[1] == ",":
                        self.take()
                self.take(")"); node = ("call", node, args)
            elif self.peek()[1] == "[" and node[0] in ("attr", "name"):
                self.take(); idx = self.expr(); self.take("]"); node = ("index", node, idx)
            else:
                return node

    def primary(self):
        kind, val = self.take()
        if kind == "num":
            return ("lit", float(val) if "." in val else int(val))
        if kind == "str":
            return ("lit", val)
        if kind == "path":
            return ("path", parse_path(val))
        if val == "(":
            node = self.expr(); self.take(")"); return node
        if val == "[":
            items = []
            while self.peek()[1] != "]":
                items.append(self.expr())
                if self.peek()[1] == ",":
                    self.take()
            self.take("]"); return ("list", items)
        if kind == "id":
            if val in ("true", "false"):
                return ("lit", val == "true")
            if val == "null":
                return ("lit", None)
            return ("name", val)
        raise ParseError(f"unexpected {val!r}")


def parse_path(text: str) -> list:
    segs, i = [], 0
    for raw in re.findall(r"\$\((?:[^()]|\([^()]*\))*\)|[^/]+", text.strip("/")):
        if raw.startswith("$("):
            segs.append(("var", Parser(tokenize(raw[2:-1])).expr()))
        else:
            segs.append(("lit", raw))
    return segs


def parse_expr(src: str):
    p = Parser(tokenize(src))
    node = p.expr()
    if p.peek()[0] != "eof":
        raise ParseError(f"trailing tokens after expression: {p.peek()}")
    return node


@dataclass
class Allow:
    ops: list[str]
    expr_src: str
    match_path: str          # e.g. "/clubs/{clubId}/members/{memberId}" relative to documents
    line: int


@dataclass
class Rules:
    functions: dict = field(default_factory=dict)   # name -> (params, expr_src)
    allows: list = field(default_factory=list)


def parse_rules(src: str) -> Rules:
    """Scan the file once, keeping a stack of open blocks (service / match / other)."""
    rules = Rules()
    clean = re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), src)
    pat = re.compile(r"service\s+[\w.]+\s*\{|match\s+(/\S+)\s*\{|function\s+(\w+)\s*\(([^)]*)\)\s*\{\s*return\s+(.*?);\s*\}"
                     r"|allow\s+([\w\s,]+?)\s*:\s*if\s+(.*?);|(\{)|(\})", re.S)
    stack: list = []
    for m in pat.finditer(clean):
        g = m.groups()
        if m.group(0).startswith("service"):
            stack.append(("service", None))
        elif g[0] is not None:
            stack.append(("match", g[0]))
        elif g[1]:
            rules.functions[g[1]] = ([p.strip() for p in g[2].split(",") if p.strip()], " ".join(g[3].split()))
        elif g[4]:
            path = "".join(v for k, v in stack if k == "match" and not v.startswith("/databases"))
            rules.allows.append(Allow([o.strip() for o in g[4].split(",")], " ".join(g[5].split()), path, src.count("\n", 0, m.start()) + 1))
        elif g[6]:
            stack.append(("other", None))
        elif g[7]:
            if stack:
                stack.pop()
    return rules


# ---------------------------------------------------------------- translation

class Unsupported(Exception):
    pass


OP_MAP = {"read": ["select"], "get": ["select"], "list": ["select"], "create": ["insert"],
          "update": ["update"], "delete": ["delete"], "write": ["insert", "update", "delete"]}
NULL = ("const", None)


@dataclass
class TableCtx:
    schema: str
    table: str                    # final table name
    coll: str                     # collection key, e.g. "clubs/members"
    columns: dict                 # Firestore field -> column name
    path_vars: dict               # {"clubId": "club_id", "memberId": "id"}


class Translator:
    """Translate one allow statement for one operation context into a SQL boolean expression."""

    def __init__(self, rules: Rules, schema_info: dict, helpers: dict):
        self.rules = rules
        self.info = schema_info            # coll -> {"table", "columns": {field: col}, "parent_col", "id_uid_col"}
        self.helpers = helpers             # name -> SQL (shared registry)

    # context: "using" (old row), "check" (new row), "insert" (new row; resource is null)
    def tr(self, node, ctx: TableCtx, mode: str, env: dict) -> str | tuple:
        k = node[0]
        if k == "lit":
            v = node[1]
            if isinstance(v, bool):
                return "true" if v else "false"
            if v is None:
                return NULL
            if isinstance(v, (int, float)):
                return str(v)
            return "'" + str(v).replace("'", "''") + "'"
        if k == "list":
            return ("list", [self.tr(x, ctx, mode, env) for x in node[1]])
        if k == "not":
            inner = self.tr(node[1], ctx, mode, env)
            if inner in ("true", "false"):
                return "false" if inner == "true" else "true"
            return f"(not {self._s(inner)})"
        if k == "bin":
            return self._bin(node, ctx, mode, env)
        if k == "name":
            n = node[1]
            if n in env:
                bound, benv = env[n]
                return self.tr(bound, ctx, mode, benv)
            if n in ctx.path_vars:
                return ctx.path_vars[n]
            raise Unsupported(f"unknown name `{n}`")
        if k == "attr":
            return self._attr(node, ctx, mode, env)
        if k == "call":
            return self._call(node, ctx, mode, env)
        if k == "index":
            raise Unsupported("map index access (x['y'])")
        if k == "path":
            raise Unsupported("bare document path outside exists()/get()")
        raise Unsupported(f"expression kind {k}")

    def _s(self, v) -> str:
        if v == NULL:
            return "null"
        if isinstance(v, tuple) and v[0] == "list":
            return "array[" + ", ".join(self._s(x) for x in v[1]) + "]"
        if isinstance(v, tuple) and v[0] == "uid":
            return "auth.uid()"
        if isinstance(v, tuple) and v[0] == "uidcol":
            return v[1]
        if isinstance(v, tuple):
            raise Unsupported(f"`{v[0]}` can't be used as a value here")
        return v

    def _bin(self, node, ctx, mode, env):
        _, op, l, r = node
        if op in ("&&", "||"):
            a, b = self.tr(l, ctx, mode, env), self.tr(r, ctx, mode, env)
            if op == "&&":
                if "false" in (a, b):
                    return "false"
                if a == "true":
                    return b
                if b == "true":
                    return a
                return f"({self._s(a)} and {self._s(b)})"
            if "true" in (a, b):
                return "true"
            if a == "false":
                return b
            if b == "false":
                return a
            return f"({self._s(a)} or {self._s(b)})"
        a, b = self.tr(l, ctx, mode, env), self.tr(r, ctx, mode, env)
        if op in ("==", "!=") and (a == NULL or b == NULL):
            other = b if a == NULL else a
            if other == NULL:
                return "true" if op == "==" else "false"
            if isinstance(other, tuple) and other[0] == "auth":
                return "auth.uid() is null" if op == "==" else "auth.uid() is not null"
            if isinstance(other, tuple) and other[0] == "nullres":
                return "false"   # resource is null on create: Firestore errors -> deny
            return f"({self._s(other)} is {'not ' if op == '!=' else ''}null)"
        if isinstance(a, tuple) and a[0] == "nullres" or isinstance(b, tuple) and b[0] == "nullres":
            return "false"   # comparing against resource.data on create: Firestore errors -> deny
        if isinstance(a, tuple) and a[0] == "auth" or isinstance(b, tuple) and b[0] == "auth":
            raise Unsupported("request.auth compared to something other than null")
        if op == "in":
            if not (isinstance(b, tuple) and b[0] == "list"):
                raise Unsupported("`in` against something other than a literal list")
            return f"({self._s(a)} = any({self._s(b)}))"
        a_uid, b_uid = isinstance(a, tuple) and a[0] == "uid", isinstance(b, tuple) and b[0] == "uid"
        if a_uid or b_uid:
            other = b if a_uid else a
            if not (isinstance(other, tuple) and other[0] in ("uidcol", "uid")):
                raise Unsupported("request.auth.uid compared to a field that is not a Firebase user id")
        sa, sb = self._s(a), self._s(b)
        if op in ("<", ">", "<=", ">="):
            return f"({sa} {op} {sb})"
        return f"({sa} {'=' if op == '==' else '<>'} {sb})"

    def _attr(self, node, ctx, mode, env):
        chain = self._chain(node)
        if chain[:2] == ["request", "auth"]:
            if len(chain) == 2:
                return ("auth",)
            if chain[2] == "uid" and len(chain) == 3:
                return ("uid",)
            if chain[2] == "token":
                raise Unsupported(f"custom auth token claim `{'.'.join(chain)}` has no equivalent in the migrated schema")
            raise Unsupported(f"`{'.'.join(chain)}`")
        if chain and chain[0] == "request" and len(chain) > 1 and chain[1] == "time":
            raise Unsupported("time-based condition (request.time)")
        if chain[:3] == ["request", "resource", "data"] and len(chain) == 4:
            if mode == "using":
                raise _NeedsNewRow()
            return self._col(ctx, chain[3])
        if chain[:2] == ["resource", "data"] and len(chain) == 3:
            if mode == "insert":
                return ("nullres",)
            if mode == "check":
                raise _NeedsOldRow()
            return self._col(ctx, chain[2])
        if node[1][0] == "attr" and node[1][2] == "data" and node[1][1][0] == "call" and node[1][1][1] == ("name", "get"):
            return self._get(node[1][1][2][0], node[2], ctx, mode, env)
        raise Unsupported(f"`{self._show(node)}`")

    def _chain(self, node) -> list[str]:
        out = []
        while node[0] == "attr":
            out.append(node[2]); node = node[1]
        if node[0] == "name":
            out.append(node[1]); return out[::-1]
        return []

    def _show(self, node) -> str:
        if node[0] == "attr":
            return self._show(node[1]) + "." + node[2]
        if node[0] == "name":
            return node[1]
        if node[0] == "call":
            return self._show(node[1]) + "(...)"
        return node[0]

    def _col(self, ctx, fieldname):
        info = self.info[ctx.coll]
        col = info["columns"].get(fieldname)
        if not col:
            raise Unsupported(f"field `{fieldname}` is not a column of {ctx.table} (array/map fields can't be used in policies)")
        if info["uid_cols"].get(fieldname):
            return ("uidcol", col)
        return col

    def _call(self, node, ctx, mode, env):
        fn = node[1]
        if fn[0] == "name" and fn[1] in self.rules.functions:
            params, body = self.rules.functions[fn[1]]
            if len(params) != len(node[2]):
                raise Unsupported(f"`{fn[1]}` called with the wrong number of arguments")
            return self.tr(parse_expr(body), ctx, mode, {p: (a, env) for p, a in zip(params, node[2])})
        if fn == ("name", "exists"):
            return self._exists(node[2][0], ctx, mode, env)
        if fn[0] == "name" and fn[1] in ("timestamp", "duration", "math", "debug"):
            raise Unsupported(f"`{fn[1]}()`")
        if fn[0] == "attr":
            raise Unsupported(f"method call `.{fn[2]}()`")
        raise Unsupported(f"call to `{self._show(fn)}`")

    def _resolve_path(self, pnode, ctx, mode, env):
        if pnode[0] != "path":
            raise Unsupported("exists()/get() on something other than a document path")
        segs = pnode[1]
        lits = [s[1] for s in segs[:3]]
        if len(segs) < 5 or lits[0] != "databases" or segs[2] != ("lit", "documents"):
            raise Unsupported("document path not under /databases/$(database)/documents")
        rest = segs[3:]
        colls, keys = [], []
        for i, s in enumerate(rest):
            if i % 2 == 0:
                if s[0] != "lit":
                    raise Unsupported("variable collection name in a path")
                colls.append(s[1])
            else:
                keys.append(s)
        coll = "/".join(colls)
        if coll not in self.info or len(keys) != len(colls):
            raise Unsupported(f"path to unknown collection `{coll}`")
        info = self.info[coll]
        args, sig = [], []
        if len(colls) > 1:
            parent_key = keys[0]
            args.append(self._s(self._seg(parent_key, ctx, mode, env)))
            sig.append(("p_parent", "text", info["parent_col"]))
        last = keys[-1]
        lv = self._seg(last, ctx, mode, env)
        if isinstance(lv, tuple) and lv[0] == "uid":
            if not info["id_uid_col"]:
                raise Unsupported(f"path keyed by request.auth.uid, but {info['table']} ids are not user ids")
            args.append("auth.uid()"); sig.append(("p_key", "uuid", info["id_uid_col"]))
        else:
            args.append(self._s(lv)); sig.append(("p_key", "text", "id"))
        return info, args, sig

    def _seg(self, seg, ctx, mode, env):
        if seg[0] == "lit":
            return "'" + seg[1].replace("'", "''") + "'"
        return self.tr(seg[1], ctx, mode, env)

    def _helper_name(self, kind, info, sig, fieldname=None):
        key = "_by_uid" if sig[-1][1] == "uuid" else ""
        return f"fb_{kind}_{info['table']}{'_' + fieldname if fieldname else ''}{key}"

    def _exists(self, pnode, ctx, mode, env):
        info, args, sig = self._resolve_path(pnode, ctx, mode, env)
        if any(a == "null" for a in args):
            return "false"
        name = self._helper_name("exists", info, sig)
        params = ", ".join(f"{p} {t}" for p, t, _ in sig)
        where = " and ".join(f"{c} = {p}" for p, _, c in sig)
        self.helpers[name] = (f"create or replace function {{S}}.{name}({params}) returns boolean language sql stable security definer "
                              f"set search_path = '' as $$ select exists (select 1 from {{S}}.{info['table']} where {where}) $$;")
        return f"{{S}}.{name}({', '.join(args)})"

    def _get(self, pnode, fieldname, ctx, mode, env):
        info, args, sig = self._resolve_path(pnode, ctx, mode, env)
        col = info["columns"].get(fieldname)
        if not col:
            raise Unsupported(f"get(...).data.{fieldname}: not a column")
        if any(a == "null" for a in args):
            return NULL
        name = self._helper_name("get", info, sig, fieldname)
        params = ", ".join(f"{p} {t}" for p, t, _ in sig)
        where = " and ".join(f"{c} = {p}" for p, _, c in sig)
        ctype = info["types"][col]
        self.helpers[name] = (f"create or replace function {{S}}.{name}({params}) returns {ctype} language sql stable security definer "
                              f"set search_path = '' as $$ select {col} from {{S}}.{info['table']} where {where} limit 1 $$;")
        return f"{{S}}.{name}({', '.join(args)})"


class _NeedsNewRow(Exception):
    pass


class _NeedsOldRow(Exception):
    pass


def match_to_ctx(match_path: str, info: dict, schema: str) -> TableCtx:
    parts = [p for p in match_path.strip("/").split("/") if p]
    colls, vars_ = parts[0::2], [p.strip("{}") for p in parts[1::2]]
    coll = "/".join(colls)
    if coll not in info:
        raise Unsupported(f"match on collection `{coll}` that has no data in the export")
    ti = info[coll]
    pv = {}
    if len(vars_) >= 1:
        pv[vars_[-1]] = "id"
    if len(vars_) >= 2:
        pv[vars_[-2]] = ti["parent_col"]
    return TableCtx(schema, ti["table"], coll, ti["columns"], pv)


def translate(rules: Rules, info: dict) -> dict:
    """Return {"policies": [...], "helpers": {name: sql}, "flags": [...]} with `{S}` as the schema placeholder."""
    helpers: dict = {}
    t = Translator(rules, info, helpers)
    policies, flags = [], []
    counter: dict = {}
    for a in rules.allows:
        try:
            ctx = match_to_ctx(a.match_path, info, "{S}")
            node = parse_expr(a.expr_src)
        except (Unsupported, ParseError) as e:
            flags.append({"rule": f"{a.match_path} allow {', '.join(a.ops)}", "line": a.line, "expr": a.expr_src, "reason": str(e),
                          "key": None}); continue
        for op in a.ops:
            for sql_op in OP_MAP.get(op, []):
                key = f"{info[ctx.coll]['coll_name']}:{'read' if sql_op == 'select' else op}"
                try:
                    if sql_op == "select" or sql_op == "delete":
                        try:
                            using = t.tr(node, ctx, "using", {}); check = None
                        except _NeedsNewRow:
                            raise Unsupported("request.resource used in a read or delete rule")
                    elif sql_op == "insert":
                        using = None; check = t.tr(node, ctx, "insert", {})
                    else:
                        try:
                            using = t.tr(node, ctx, "using", {})
                        except _NeedsNewRow:
                            using = "true"
                        try:
                            check = t.tr(node, ctx, "check", {})
                        except _NeedsOldRow:
                            check = "true"
                        if using == "true" and check == "true" and _mentions_data(a.expr_src):
                            raise Unsupported("update rule compares old and new data in one condition")
                except Unsupported as e:
                    flags.append({"rule": f"{a.match_path} allow {op}", "line": a.line, "expr": a.expr_src, "reason": str(e), "key": key})
                    continue
                expr = using if sql_op in ("select", "delete") else check
                if expr == "false" or (sql_op == "update" and using == "false"):
                    policies.append({"table": ctx.table, "op": sql_op, "name": None, "deny": True, "source": f"line {a.line}: allow {op}: if {a.expr_src}", "key": key})
                    continue
                n = counter[(ctx.table, sql_op)] = counter.get((ctx.table, sql_op), 0) + 1
                name = f"fb_{ctx.table}_{sql_op}_{n}"
                to = "authenticated, anon"
                sql = f"create policy {name} on {{S}}.{ctx.table} for {sql_op} to {to}"
                if sql_op in ("select", "delete"):
                    sql += f" using ({t._s(using)})"
                elif sql_op == "insert":
                    sql += f" with check ({t._s(check)})"
                else:
                    sql += f" using ({t._s(using)}) with check ({t._s(check)})"
                policies.append({"table": ctx.table, "op": sql_op, "name": name, "sql": sql + ";", "deny": False,
                                 "source": f"line {a.line}: allow {op}: if {a.expr_src}", "key": key})
    return {"policies": policies, "helpers": helpers, "flags": flags}


def _mentions_data(src: str) -> bool:
    return "resource.data" in src
