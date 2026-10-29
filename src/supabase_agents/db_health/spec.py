"""Courtline's intended access rules for every table in `health`: the yardstick for RLS checks.

Each table has:
  visibility   'public' | 'club' | 'own' | 'own_or_staff' | 'staff'
  policy       the USING expression a correct SELECT policy should have ({s} = schema)
  expected     SQL (run as postgres, bypassing RLS) listing the row ids user %(uid)s SHOULD see
A probe compares what each test user actually sees under RLS with `expected`.
"""

from __future__ import annotations

CLUB_OF = "select club_id from {s}.members where user_id = %(uid)s"
STAFF_CLUB_OF = "select club_id from {s}.members where user_id = %(uid)s and role = 'staff'"
OWN_MEMBER_IDS = "select id from {s}.members where user_id = %(uid)s"

ACCESS = {
    "clubs": {"visibility": "public", "policy": "true",
              "expected": "select id::text from {s}.clubs"},
    "members": {"visibility": "own", "policy": "user_id = (select auth.uid())",
                "expected": "select id::text from {s}.members where user_id = %(uid)s"},
    "courts": {"visibility": "club", "policy": "{s}.is_member(club_id)",
               "expected": f"select id::text from {{s}}.courts where club_id in ({CLUB_OF})"},
    "coaches": {"visibility": "club", "policy": "{s}.is_member(club_id)",
                "expected": f"select id::text from {{s}}.coaches where club_id in ({CLUB_OF})"},
    "announcements": {"visibility": "club", "policy": "{s}.is_member(club_id)",
                      "expected": f"select id::text from {{s}}.announcements where club_id in ({CLUB_OF})"},
    "leagues": {"visibility": "club", "policy": "{s}.is_member(club_id)",
                "expected": f"select id::text from {{s}}.leagues where club_id in ({CLUB_OF})"},
    "bookings": {"visibility": "own",
                 "policy": "member_id in (select m.id from {s}.members m where m.user_id = (select auth.uid()))",
                 "expected": f"select id::text from {{s}}.bookings where member_id in ({OWN_MEMBER_IDS})"},
    "payments": {"visibility": "own_or_staff",
                 "policy": "member_id in (select m.id from {s}.members m where m.user_id = (select auth.uid())) or {s}.is_staff(club_id)",
                 "expected": f"select id::text from {{s}}.payments where member_id in ({OWN_MEMBER_IDS}) or club_id in ({STAFF_CLUB_OF})"},
    "lesson_notes": {"visibility": "staff", "policy": "{s}.is_staff(club_id)",
                     "expected": f"select id::text from {{s}}.lesson_notes where club_id in ({STAFF_CLUB_OF})"},
}

CLEAN_TABLES = ("clubs", "courts", "coaches", "announcements")


def policy_expr(table: str, schema: str) -> str:
    return ACCESS[table]["policy"].format(s=schema)


def expected_sql(table: str, schema: str) -> str:
    return ACCESS[table]["expected"].format(s=schema)
