"""Generates fixtures/firebase/export.json: a fictional Firestore export for 3 Courtline clubs.

Format (documented in docs/firebase-migration.md): {"<collection>": {"<docId>": {"fields": {...},
"__collections__": {"<sub>": {...}}}}}. Typed values use Firestore's export shapes:
  {"__type__": "timestamp", "seconds": int, "nanos": int}
  {"__type__": "ref", "path": "clubs/club_oak/courts/c1"}
Planted problems (labelled in fixtures/firebase/expected.json): numeric strings, a non-numeric rating, a missing
field, an ISO-string timestamp, orphaned refs, an orphan inside an array of refs, and an exact duplicate doc.
Deterministic: run it and you get the same file.
    uv run python -m supabase_agents.firebase_migration.fixture_gen
"""

from __future__ import annotations

import json
import random

from ..core.config import data_dir

T0 = 1790000000  # 2026-09-21 14:13 UTC


def ts(sec: int) -> dict:
    return {"__type__": "timestamp", "seconds": sec, "nanos": 0}


def ref(path: str) -> dict:
    return {"__type__": "ref", "path": path}


CLUBS = [("club_oak", "Oakridge Tennis Club", "Austin", "pro", ["tennis"]),
         ("club_bay", "Bayside Pickleball", "Tampa", "starter", ["pickleball"]),
         ("club_elm", "Elmwood Racquet Club", "Denver", "pro", ["tennis", "pickleball"])]

# Named identities the rules tests use (firebase uids)
OWNERS = {"club_oak": "fbOwnOak9Qx2", "club_bay": "fbOwnBay4Lm7", "club_elm": "fbOwnElm1Zt8"}
NAMED = {
    "club_oak": [("fbStaffOak3Kd", "Dana Reyes", "staff"), ("fbPlayOak1Aa", "Chris Lowe", "player"),
                 ("fbPlayOak2Bb", "Mina Park", "player")],
    "club_bay": [("fbStaffBay5Hq", "Ola Grant", "staff"), ("fbPlayBay1Cc", "Theo Banks", "player")],
    "club_elm": [("fbStaffElm8Rw", "Ruth Kim", "admin")],
}
FIRST = ["Avery", "Jules", "Kai", "Noor", "Pat", "Reese", "Sky", "Tam", "Wren", "Zia", "Lee", "Remy"]
LAST = ["Hale", "Ito", "Moss", "Nash", "Ortiz", "Pike", "Quinn", "Rowe", "Sato", "Vale"]


def build() -> dict:
    rnd = random.Random(7)
    out: dict = {"clubs": {}, "bookings": {}, "payments": {}, "leagues": {}}
    members_by_club: dict[str, list[str]] = {}
    courts_by_club: dict[str, list[str]] = {}
    for i, (cid, name, city, plan, sports) in enumerate(CLUBS):
        owner = OWNERS[cid]
        members: dict = {}
        roster = [(owner, f"{name.split()[0]} Owner", "admin")] + NAMED[cid]
        for k in range(5):
            uid = f"fb{cid[5:].title()}{k}{rnd.choice('ABCDEFGHJK')}{rnd.randint(10, 99)}"
            roster.append((uid, f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", "player"))
        courts = {}
        for c in range(1, 4 if cid != "club_elm" else 5):
            cfields = {"name": f"Court {c}", "surface": rnd.choice(["hard", "clay", "acrylic"]),
                       "indoor": c == 1, "hourly_rate": float(20 + 5 * c)}
            courts[f"c{c}"] = {"fields": cfields}
        # planted: numeric string rate, missing indoor flag
        if cid == "club_bay":
            courts["c2"]["fields"]["hourly_rate"] = "30"
            del courts["c3"]["fields"]["indoor"]
        courts_by_club[cid] = list(courts)
        for j, (uid, display, role) in enumerate(roster):
            f = {"uid": uid, "display_name": display, "email": f"{display.split()[0].lower()}.{j}@{cid[5:]}.example",
                 "role": role, "joined_at": ts(T0 - 86400 * (300 - 20 * j)),
                 "dupr_rating": round(3.0 + rnd.random() * 2, 2)}
            if j % 3 != 1:
                f["phone"] = f"555-01{rnd.randint(10, 99)}"
            if j % 2 == 0:
                f["favorite_courts"] = [ref(f"clubs/{cid}/courts/{c}") for c in list(courts)[:2]]
            members[uid] = {"fields": f}
        members_by_club[cid] = list(members)
        out["clubs"][cid] = {"fields": {"name": name, "city": city, "plan": plan, "owner_uid": owner,
                                        "created_at": ts(T0 - 86400 * (400 + 30 * i)), "sports": sports,
                                        "settings": {"booking_window_days": 7 + i, "theme": ["green", "blue", "red"][i],
                                                     **({"guest_fee_cents": 1500} if i == 0 else {"waitlist": True})}},
                             "__collections__": {"members": members, "courts": courts}}
    # planted: two ratings typed as strings, one unusable
    oak = out["clubs"]["club_oak"]["__collections__"]["members"]
    oak["fbPlayOak1Aa"]["fields"]["dupr_rating"] = "4.5"
    oak["fbPlayOak2Bb"]["fields"]["dupr_rating"] = "3.75"
    out["clubs"]["club_bay"]["__collections__"]["members"]["fbPlayBay1Cc"]["fields"]["dupr_rating"] = "n/a"

    n = 0
    for cid in [c[0] for c in CLUBS]:
        mem = members_by_club[cid]
        for k in range(12):
            n += 1
            m = mem[1 + (k % (len(mem) - 1))]
            court = courts_by_club[cid][k % len(courts_by_club[cid])]
            start = T0 + 86400 * (k + 1) + 3600 * (8 + k % 10)
            players = [ref(f"clubs/{cid}/members/{p}") for p in rnd.sample(mem, 2)]
            f = {"club_id": cid, "court": ref(f"clubs/{cid}/courts/{court}"), "member_uid": m,
                 "starts_at": ts(start), "ends_at": ts(start + 3600), "status": rnd.choice(["confirmed", "confirmed", "cancelled"]),
                 "players": players}
            if k % 4 == 0:
                f["notes"] = rnd.choice(["Bring extra balls", "Lesson with coach", "Doubles"])
            out["bookings"][f"bk{n:03d}"] = {"fields": f}
    # planted: P1 and P2 at Oakridge each own a known booking (used by the rules tests)
    out["bookings"]["bk_p1"] = {"fields": {"club_id": "club_oak", "court": ref("clubs/club_oak/courts/c1"), "member_uid": "fbPlayOak1Aa",
                                           "starts_at": ts(T0 + 86400 * 3), "ends_at": ts(T0 + 86400 * 3 + 3600), "status": "confirmed",
                                           "players": [ref("clubs/club_oak/members/fbPlayOak1Aa")]}}
    out["bookings"]["bk_p2"] = {"fields": {"club_id": "club_oak", "court": ref("clubs/club_oak/courts/c2"), "member_uid": "fbPlayOak2Bb",
                                           "starts_at": ts(T0 + 86400 * 4), "ends_at": ts(T0 + 86400 * 4 + 3600), "status": "confirmed",
                                           "players": [ref("clubs/club_oak/members/fbPlayOak2Bb")]}}
    # planted: orphan court (deleted court c9), ISO-string timestamp, orphan inside players, exact duplicate
    out["bookings"]["bk_orphan"] = {"fields": {**out["bookings"]["bk005"]["fields"], "court": ref("clubs/club_oak/courts/c9")}}
    out["bookings"]["bk010"]["fields"]["starts_at"] = "2026-10-02T15:00:00Z"
    out["bookings"]["bk020"]["fields"]["players"] = out["bookings"]["bk020"]["fields"]["players"] + [ref("clubs/club_bay/members/fbGone0X99")]
    out["bookings"]["bk_dup"] = json.loads(json.dumps(out["bookings"]["bk007"]))

    p = 0
    for cid in [c[0] for c in CLUBS]:
        for k in range(8):
            p += 1
            m = members_by_club[cid][1 + (k % (len(members_by_club[cid]) - 1))]
            f = {"club_id": cid, "member": ref(f"clubs/{cid}/members/{m}"), "member_uid": m,
                 "amount_cents": 2500 + 500 * (k % 4), "currency": "usd",
                 "method": {"type": "card" if k % 3 else "ach", "last4": f"{4000 + 7 * k + p:04d}"[-4:]},
                 "paid_at": ts(T0 + 3600 * (k + 10 * p)), "metadata": {"source": rnd.choice(["app", "front_desk"])}}
            if k % 5 == 0:
                f["metadata"]["promo"] = "FALL25"
            if k == 3:
                f["refunded"] = True
            out["payments"][f"pay{p:03d}"] = {"fields": f}
    out["payments"]["pay_p1"] = {"fields": {"club_id": "club_oak", "member": ref("clubs/club_oak/members/fbPlayOak1Aa"), "member_uid": "fbPlayOak1Aa",
                                            "amount_cents": 4000, "currency": "usd", "method": {"type": "card", "last4": "4242"},
                                            "paid_at": ts(T0 + 7200), "metadata": {"source": "app"}}}
    # planted: integral float amount, orphan member ref
    out["payments"]["pay004"]["fields"]["amount_cents"] = 4000.0
    out["payments"]["pay_orphan"] = {"fields": {**out["payments"]["pay009"]["fields"], "member": ref("clubs/club_bay/members/fbGone0X99"),
                                                "member_uid": "fbGone0X99"}}

    for i, (lid, name) in enumerate([("lg_fall", "Fall Ladder"), ("lg_mixed", "Mixed Doubles"), ("lg_junior", "Junior Open")]):
        out["leagues"][lid] = {"fields": {"name": name, "club_id": CLUBS[i][0], "season_ends": ts(T0 + 86400 * (60 + 30 * i)),
                                          "max_teams": 8 + 4 * i}}
    return out


def main() -> None:
    path = data_dir("firebase", "export.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
