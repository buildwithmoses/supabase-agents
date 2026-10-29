"""Offline tests for the Firebase -> Supabase migration assistant: no database, no keys."""

import json
from datetime import datetime, timezone

import pytest

from supabase_agents.core.config import data_dir
from supabase_agents.firebase_migration.export import coll_key_of_path, load, walk
from supabase_agents.firebase_migration.graph import IDENT, numbers_ok
from supabase_agents.firebase_migration.infer import _kind, convert_rows, infer, uid_to_uuid
from supabase_agents.firebase_migration.rules import ParseError, Unsupported, parse_expr, parse_rules, translate
from supabase_agents.firebase_migration.schema_info import policy_info
from supabase_agents.firebase_migration.sqlgen import lit, migration_sql

TS = {"__type__": "timestamp", "seconds": 1790000000, "nanos": 0}


@pytest.fixture(scope="module")
def world():
    docs = walk(load(data_dir("firebase", "export.json")))
    schema = infer(docs)
    conv = convert_rows(docs, schema)
    tr = translate(parse_rules(data_dir("firebase", "firestore.rules").read_text()), policy_info(schema))
    return docs, schema, conv, tr


# ---- type inference

def test_kinds():
    assert _kind([TS, "2026-10-02T15:00:00Z"])[0] == "timestamptz"
    assert _kind([1, 2, 3.0])[0] == "bigint"
    assert _kind([1.5, 2.0])[0] == "double precision"
    assert _kind([20.0, 25.0, "30"])[0] == "double precision"
    assert _kind([True, False])[0] == "boolean"
    assert _kind([["a"], ["b", "c"]])[0] == "text[]"
    assert _kind([{"__type__": "ref", "path": "clubs/a/courts/c1"}])[1]["target"] == "clubs/courts"
    assert _kind([[{"__type__": "ref", "path": "clubs/a/members/u"}]])[0] == "ref_array"
    assert _kind([{"a": 1, "b": "x"}] * 10)[0] == "flatten"
    assert _kind([{"a": 1}, {"b": 2}, {"c": 3}])[0] == "jsonb"


def test_schema_decisions(world):
    _, schema, _, _ = world
    t = schema["tables"]
    cols = {c["name"]: c for c in t["payments"]["columns"]}
    assert cols["metadata"]["type"] == "jsonb" and cols["method_type"]["kind"] == "flatten"
    assert cols["member_id"]["type"] == "uuid" and cols["member_path"]["fk"]["coll"] == "clubs/members"
    assert cols["club_id"]["fk"] == {"coll": "clubs", "col": "id"}
    assert [j["name"] for j in t["bookings"]["join"]] == ["bookings_players"]
    assert t["clubs/members"]["id_is_uid_column"] == "user_id"
    m = {c["name"]: c for c in t["clubs/members"]["columns"]}
    assert m["dupr_rating"]["nullable"] and m["phone"]["nullable"] and not m["email"]["nullable"]
    assert schema["order"].index("clubs") < schema["order"].index("clubs/members") < schema["order"].index("payments")


def test_skips_and_fixes_match_labels(world):
    _, _, conv, _ = world
    exp = json.loads(data_dir("firebase", "expected.json").read_text())
    assert {s["path"]: s["reason"] for s in conv["skips"]} == exp["must_skip"]
    assert {f["where"] for f in conv["fixes"]} == set(exp["must_fix"])
    rows = {r["_path"]: r for r in conv["rows"]["bookings"]}
    assert rows["bookings/bk010"]["starts_at"] == datetime(2026, 10, 2, 15, tzinfo=timezone.utc)


def test_duplicates_are_per_parent():
    docs = [{"coll": "clubs/courts", "path": f"clubs/{c}/courts/c1", "id": "c1", "parents": [("clubs", c)], "fields": {"name": "Court 1"}}
            for c in ("a", "b")] + [{"coll": "clubs", "path": f"clubs/{c}", "id": c, "parents": [], "fields": {"name": c}} for c in ("a", "b")]
    conv = convert_rows(docs, infer(docs))
    assert conv["skips"] == []


def test_uid_mapping_is_stable():
    assert uid_to_uuid("fbPlayOak1Aa") == uid_to_uuid("fbPlayOak1Aa") != uid_to_uuid("fbPlayOak2Bb")


def test_coll_key():
    assert coll_key_of_path("clubs/x/members/u1") == "clubs/members"


# ---- rules parsing and translation

def test_parse_rules_structure():
    r = parse_rules(data_dir("firebase", "firestore.rules").read_text())
    assert set(r.functions) == {"isSignedIn", "isMember", "isStaff"}
    assert {a.match_path for a in r.allows} >= {"/clubs/{clubId}/members/{memberId}", "/bookings/{bookingId}"}


def test_translation_of_known_patterns(world):
    _, _, _, tr = world
    sql = {p["name"]: p["sql"] for p in tr["policies"] if not p["deny"]}
    assert "auth.uid() is not null" in sql["fb_clubs_select_1"]
    assert "(auth.uid() = member_id)" in sql["fb_bookings_select_1"]
    assert "fb_get_members_role_by_uid(club_id, auth.uid()) = any(array['staff', 'admin'])" in sql["fb_bookings_select_1"]
    assert "fb_exists_members_by_uid(club_id, auth.uid())" in sql["fb_bookings_insert_1"]
    assert "with check (true)" in sql["fb_bookings_update_1"]
    # create on clubs compares to resource.data -> null on create -> denied, so no insert policy
    assert not any(p["table"] == "clubs" and p["op"] == "insert" and not p["deny"] for p in tr["policies"])
    assert all("security definer" in h for h in tr["helpers"].values())


def test_unsupported_patterns_are_flagged_not_guessed(world):
    _, _, _, tr = world
    keys = {f["key"] for f in tr["flags"]}
    assert keys == {"payments:update", "leagues:read"}
    assert not any(p["table"] in ("leagues",) for p in tr["policies"])
    assert not any(p["table"] == "payments" and p["op"] == "update" for p in tr["policies"])


@pytest.mark.parametrize("expr", ["request.time < timestamp.date(2026, 1, 1)", "request.resource.data.keys().hasOnly(['a'])",
                                  "request.auth.token.admin == true", "request.auth.uid == resource.data.name"])
def test_each_unsupported_pattern(expr, world):
    _, schema, _, _ = world
    src = "service cloud.firestore { match /databases/{database}/documents { match /bookings/{b} { allow read: if " + expr + "; } } }"
    tr = translate(parse_rules(src), policy_info(schema))
    assert tr["policies"] == [] and len(tr["flags"]) == 1


def test_false_means_no_policy(world):
    _, schema, _, _ = world
    src = "service cloud.firestore { match /databases/{database}/documents { match /payments/{p} { allow delete: if false; } } }"
    tr = translate(parse_rules(src), policy_info(schema))
    assert tr["policies"][0]["deny"] and not tr["flags"]


def test_parser_errors():
    with pytest.raises(ParseError):
        parse_expr("a == ")
    node = parse_expr("exists(/databases/$(database)/documents/clubs/$(clubId)/members/$(request.auth.uid))")
    assert node[0] == "call" and node[2][0][0] == "path"


# ---- SQL generation and report checks

def test_literals():
    assert lit("O'Brien") == "'O''Brien'"
    assert lit({"a": 1}, "jsonb") == "'{\"a\": 1}'::jsonb"
    assert lit(["tennis"], "text[]") == "array['tennis']::text[]"
    assert lit(None) == "null" and lit(True) == "true"
    assert lit("5b2f8f0a-0000-0000-0000-000000000000", "uuid").endswith("::uuid")


def test_migration_sql_has_schema_placeholder_and_flags(world):
    _, schema, conv, tr = world
    sql = migration_sql(schema, tr, conv)
    assert "create table {S}.bookings" in sql and "enable row level security" in sql
    assert sql.count("-- FLAGGED") == 2
    assert "migration." not in sql


def test_numbers_check():
    facts = {"rows": 103, "skipped": 3}
    assert numbers_ok("103 rows loaded, 3 skipped", facts) == []
    assert numbers_ok("104 rows loaded", facts) == ["104"]


def test_rename_validation():
    assert IDENT.match("club_members") and not IDENT.match("Club Members") and not IDENT.match("1x")
