# Firestore -> Supabase migration report (20261029T141848-3c4b)

The migration loaded 103 rows across 6 tables and 2 join tables, with 16 policies. 3 rows were skipped: bookings/bk_dup (duplicate of bk007), bookings/bk_orphan (missing court c9), and payments/pay_orphan (missing member). 7 values were fixed, including timestamp parsing, dropping a missing player reference, numeric string conversions, a non-numeric DUPR rating set to NULL, and an integral float stored as an integer. No proof result was recorded, so verification is not confirmed. The payments:update and leagues:read rules are flagged and need human review before approval.

Summary written by: model

## Proof (trial schema)

Not run.

## Skipped documents

- `bookings/bk_dup`: duplicate (identical to bookings/bk007)
- `bookings/bk_orphan`: orphan_ref (court -> clubs/club_oak/courts/c9 does not exist)
- `payments/pay_orphan`: orphan_ref (member -> clubs/club_bay/members/fbGone0X99 does not exist)

## Changed values

- `bookings/bk010.starts_at`: ISO string parsed as timestamp
- `bookings/bk020.players`: reference to a missing document dropped from the array
- `clubs/club_bay/courts/c2.hourly_rate`: numeric string converted
- `clubs/club_bay/members/fbPlayBay1Cc.dupr_rating`: non-numeric value set to NULL
- `clubs/club_oak/members/fbPlayOak1Aa.dupr_rating`: numeric string converted
- `clubs/club_oak/members/fbPlayOak2Bb.dupr_rating`: numeric string converted
- `payments/pay004.amount_cents`: integral float stored as integer

## Rules that need a human

- payments:update
- leagues:read

## Decisions explained

- **bookings.club_id**: Every value matches an existing club, so a foreign key to clubs was added safely. Bookings for a club cannot point at a club that does not exist.
- **bookings.member_uid**: The Firebase user id became a uuid column named member_id, generated with uuid5 from the Firebase uid, so row-level security can compare it to auth.uid(). Check that the Supabase auth accounts use the same uuid5 mapping, or the comparison will never match.
- **bookings.starts_at**: The field held a mix of Firestore timestamps and ISO strings, and the strings were parsed into a single timestamp type. Any string in an unexpected format would have failed, so confirm the parsed times look right.
- **clubs.owner_uid**: Same approach as the member ids: a uuid named owner_id, derived with uuid5 from the Firebase uid, so ownership checks in row-level security work against auth.uid(). The same mapping must be used when creating auth users.
- **clubs.settings**: Its keys vary from document to document, so it is stored as jsonb rather than fixed columns. Queries on individual settings will need jsonb operators, and no column constraints apply to it.
- **courts.hourly_rate**: Some rates were numbers and one was a numeric string, which was converted. This is safe, but the rate column should be reviewed to confirm the converted value is correct.
- **members.dupr_rating**: Two numeric strings were converted and one non-numeric value was set to NULL. The NULL is a data loss of that one value, so check whether the original text should be kept elsewhere.
- **members.uid**: The Firebase user id became a uuid named user_id, generated with uuid5, so row-level security can match it to auth.uid(). This depends on the same mapping being used for auth users.
- **leagues.club_id**: Every value matches an existing club, so a foreign key to clubs was added safely. Leagues cannot reference a missing club.
- **payments.club_id**: Every value matches an existing club, so a foreign key to clubs was added safely. Payments cannot reference a missing club.
- **payments.member_uid**: The Firebase user id became a uuid named member_id, generated with uuid5, so row-level security can compare it to auth.uid(). It relies on the same mapping as the other user columns.
- **payments.metadata**: Its keys vary across documents, so it is stored as jsonb. Expect to query it with jsonb operators, and any field you need often should be promoted to a real column later.
- **payments.method**: At least 90 percent of documents share the keys last4 and type, so those were flattened into regular columns. Documents missing those keys will have NULLs, which is expected.
- **bookings.players**: This was an array of references, so it became a join table, bookings_players. This keeps player lists queryable and enforces relationships with foreign keys.
- **members.favorite_courts**: This was an array of references, so it became a join table, members_favorite_courts. This keeps favorites queryable and enforces that each court exists.

## Risks

- No proof result was recorded, so the migrated data's correctness is unverified at cutover.
- The payments:update and leagues:read rules are flagged and need a human to confirm they behave correctly.
- Skipped orphan bookings and payments may leave gaps in history where referenced courts or members no longer exist.
- Fixed values, including a NULL DUPR rating and a dropped player reference, changed source data and should be spot-checked.

## Cutover checklist

1. Freeze writes on the Firebase clubs (deploy read-only rules) at the cutover time and announce a 30-minute window.
2. Re-export Firestore after the freeze and re-run this migration; the proof must pass again on the fresh export.
3. Import Firebase Auth users into Supabase Auth with the same uuid5 ids this migration used (24 distinct users referenced), so auth.uid() matches every migrated row.
4. Write the policy for flagged rule payments:update by hand (custom auth token claim `request.auth.token.admin` has no equivalent in the migrated schema). Until then that access is denied.
5. Write the policy for flagged rule leagues:read by hand (custom auth token claim `request.auth.token.email_verified` has no equivalent in the migrated schema). Until then that access is denied.
6. Update the app: array fields now live in join tables (bookings_players, members_favorite_courts), which are read-only to users until write policies are added.
7. Point the app's reads and writes at Supabase, keep Firebase read-only for 7 days as the rollback path.
8. Run the Supabase security and performance advisors on the new schema before opening traffic.

Migration SQL: `/Users/henrymoses/Desktop/buildwithmoses-automations/company-based-builds/supabase/supabase-agents/evals/firebase_migration/out/migration-20261029T141848-3c4b.sql`
