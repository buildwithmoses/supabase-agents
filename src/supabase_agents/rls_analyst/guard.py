"""The SQL guard: code, not the prompt, decides what may run.

review(sql) -> Decision(ok, sql_to_run, reasons, tables, contact_columns)

Blocks: anything but one SELECT (or WITH ... SELECT / UNION of selects); write or DDL nodes anywhere, including
inside CTEs; SELECT INTO; locking clauses; tables outside the `analyst` schema (auth, public, pg_catalog,
information_schema, extensions ...); any function not on a short allow-list, which covers set_config,
current_setting, pg_sleep, pg_read_file, dblink, lo_*, the policy helpers and every pg_* function;
statements longer than 4,000 characters. Enforces LIMIT 200. Fails closed on anything it can't parse.

The guard is the second line. The first is that every query runs as the asking user under RLS (executor.py),
so even a query the guard missed can only see that user's rows. The guard exists because inside the user's
own transaction, set_config('request.jwt.claims', ...) would let a query impersonate someone else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

logging.getLogger("sqlglot").setLevel(logging.ERROR)

SCHEMA = "analyst"
TABLES = {"clubs", "profiles", "memberships", "contacts", "courts", "bookings", "payments", "leagues", "league_entries"}
CONTACT_COLUMNS = {"email", "phone"}
MAX_ROWS = 200
MAX_SQL_CHARS = 4000

# Typed sqlglot function nodes that are safe to compute on rows the user can already see.
SAFE_FUNC_NODES = (
    exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Round, exp.Coalesce, exp.Nullif, exp.Abs, exp.Floor, exp.Ceil,
    exp.Cast, exp.TryCast, exp.Case, exp.If, exp.Extract, exp.TimestampTrunc, exp.DateTrunc, exp.CurrentDate,
    exp.CurrentTimestamp, exp.Lower, exp.Upper, exp.Concat, exp.ConcatWs, exp.Length, exp.Trim, exp.Substring,
    exp.Greatest, exp.Least, exp.TimeToStr, exp.GroupConcat, exp.ArrayAgg, exp.Stddev, exp.Variance,
    exp.PercentileCont, exp.PercentileDisc, exp.RowNumber, exp.Rank, exp.DenseRank, exp.Lag, exp.Lead, exp.DateAdd,
    exp.DateSub, exp.DateDiff, exp.Pow, exp.Sqrt, exp.Div, exp.Mod, exp.Interval, exp.StrToTime, exp.Date,
    exp.First, exp.Last, exp.Exists, exp.Initcap, exp.Left, exp.Right, exp.Replace,
)
# Untyped (Anonymous) functions sqlglot doesn't model: lower-case names allowed.
SAFE_ANON = {"date_part", "to_char", "now", "age", "string_agg", "initcap", "split_part", "make_interval", "date",
             "timezone", "justify_days", "percent_rank", "cume_dist", "ntile", "bool_or", "bool_and", "count_if"}
WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command, exp.Merge,
               exp.TruncateTable, exp.Into, exp.Lock, exp.Set, exp.Transaction, exp.Commit, exp.Rollback, exp.Copy,
               exp.Grant if hasattr(exp, "Grant") else exp.Command)
DENY_WORDS = {"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "GRANT", "REVOKE", "TRUNCATE", "COPY", "SET",
              "RESET", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "DO", "CALL", "EXECUTE", "PREPARE", "LISTEN",
              "NOTIFY", "VACUUM", "ANALYZE", "LOCK", "INTO"}
READ_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)


@dataclass
class Decision:
    ok: bool
    sql: str
    sql_to_run: str = ""
    reasons: list[str] = field(default_factory=list)
    tables: set[str] = field(default_factory=set)
    contact_columns: bool = False

    def to_dict(self) -> dict:
        return {"ok": self.ok, "sql": self.sql, "sql_to_run": self.sql_to_run, "reasons": self.reasons,
                "tables": sorted(self.tables), "contact_columns": self.contact_columns}


def _block(sql: str, reason: str) -> Decision:
    return Decision(ok=False, sql=sql, reasons=[reason])


def review(sql: str) -> Decision:
    if not isinstance(sql, str) or not sql.strip():
        return _block(sql or "", "Empty query.")
    if len(sql) > MAX_SQL_CHARS:
        return _block(sql, f"Query longer than {MAX_SQL_CHARS} characters.")
    if "\x00" in sql or "$$" in sql:
        return _block(sql, "Query contains a NUL byte or dollar-quoted string.")
    try:
        tokens = sqlglot.tokenize(sql, read="postgres")
    except Exception as e:  # noqa: BLE001 - fail closed
        return _block(sql, f"Could not tokenize: {e}")
    semis = [i for i, t in enumerate(tokens) if t.token_type == TokenType.SEMICOLON]
    if any(any(t.token_type != TokenType.SEMICOLON for t in tokens[i + 1:]) for i in semis):
        return _block(sql, "Only one statement is allowed.")
    words = [t for t in tokens if t.token_type not in (TokenType.STRING, TokenType.IDENTIFIER, TokenType.NATIONAL_STRING)]
    for t in words:
        if t.text.upper() in DENY_WORDS:
            return _block(sql, f"'{t.text.upper()}' is not allowed. Only a single read-only SELECT runs.")
    if not words or words[0].text.upper() not in ("SELECT", "WITH", "("):
        return _block(sql, "Only SELECT or WITH ... SELECT queries are allowed.")
    try:
        parsed = [p for p in sqlglot.parse(sql, read="postgres") if p is not None and not isinstance(p, exp.Semicolon)]
    except Exception as e:  # noqa: BLE001
        return _block(sql, f"Could not parse the query: {str(e).splitlines()[0][:200]}")
    if len(parsed) != 1:
        return _block(sql, "Only one statement is allowed.")
    root = parsed[0]
    if isinstance(root, exp.Subquery):
        root = root.unnest()
    if not isinstance(root, READ_ROOTS):
        return _block(sql, f"Only SELECT queries are allowed (got {type(root).__name__}).")

    ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    tables: set[str] = set()
    for node in root.walk():
        if isinstance(node, WRITE_NODES):
            return _block(sql, f"Read-only: found {type(node).__name__} in the query.")
        if isinstance(node, exp.Table):
            name = (node.name or "").lower()
            db = (node.args.get("db").name.lower() if node.args.get("db") else "")
            if node.args.get("catalog"):
                return _block(sql, "Cross-database references are not allowed.")
            if not name:
                return _block(sql, "Table functions are not allowed.")
            if db in ("", SCHEMA) and name in TABLES:
                tables.add(name)
            elif not db and name in ctes:
                pass
            else:
                return _block(sql, f"Table {db + '.' if db else ''}{name} is outside the analyst schema.")
        if isinstance(node, exp.Func) and not isinstance(node, (exp.Binary, exp.Unary, exp.Connector, exp.Not, exp.Paren)):
            if isinstance(node, exp.Anonymous):
                fname = str(node.name).lower()
                if fname not in SAFE_ANON:
                    return _block(sql, f"Function {fname}() is not allowed.")
            elif not isinstance(node, SAFE_FUNC_NODES):
                return _block(sql, f"Function {node.sql_name().lower()}() is not allowed.")
        if isinstance(node, exp.Dot) and isinstance(node.this, exp.Identifier):
            # schema-qualified function call such as auth.uid() or analyst.my_club_ids()
            return _block(sql, f"Schema-qualified function {node.sql()} is not allowed.")
    if not tables:
        return _block(sql, "The query must read at least one analyst table.")

    contact = "contacts" in tables and any(
        (c.name or "").lower() in CONTACT_COLUMNS for c in root.find_all(exp.Column)) or (
        "contacts" in tables and any(isinstance(s, exp.Star) and not isinstance(s.parent, exp.Count) for s in root.find_all(exp.Star)))

    # Enforce a row limit.
    if isinstance(root, exp.Select):
        lim = root.args.get("limit")
        n = None
        try:
            n = int(lim.expression.name) if lim is not None and lim.expression is not None else None
        except Exception:  # noqa: BLE001
            n = None
        if n is None or n > MAX_ROWS:
            root = root.limit(MAX_ROWS)
        run = root.sql(dialect="postgres")
    else:
        run = f"select * from ({root.sql(dialect='postgres')}) as q limit {MAX_ROWS}"
    return Decision(ok=True, sql=sql, sql_to_run=run, tables=tables, contact_columns=bool(contact))
