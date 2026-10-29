# Firestore -> Supabase migration report (20261029T144646-33e0)

Migration loaded 103 rows, skipped 3 (one duplicate booking and two orphan references), and applied 7 value fixes across 6 tables and 2 join tables. The proof passed: all 33 regular rules tests matched Firebase, and both flagged rule tests were denied as intended. Fidelity is 1.0 across 819 checked fields with 0 orphans. Human review is needed for the flagged rules payments:update and leagues:read, and for the 16 policies overall before approval.

Summary written by: model

## Proof (trial schema)

| Table | Source docs | Rows | Skipped | Reconciled |
|---|---|---|---|---|
| bookings | 40 | 38 | 2 | yes |
| clubs | 3 | 3 | 0 | yes |
| courts | 10 | 10 | 0 | yes |
| members | 24 | 24 | 0 | yes |
| leagues | 3 | 3 | 0 | yes |
| payments | 26 | 25 | 1 | yes |

- Referential integrity: 0 orphans across 11 foreign keys
- Field fidelity: 1.0 over 819 fields
- Rules tests: 1.0 match Firebase on 33 tests; flagged rules fail closed: 1.0

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

- **bookings.club_id**: Every value already matches an existing club, so a foreign key to clubs was added without dropping any rows. Bookings tied to a club cannot now point at a missing club.
- **bookings.member_uid**: The Firebase user id is converted to a uuid (member_id) using uuid5, so row-level security can compare it with auth.uid(). The conversion is one-way, so the auth system must derive the same uuid from the Firebase uid or the policies will silently block access.
- **bookings.starts_at**: The column mixed Firestore timestamps with ISO strings. The strings were parsed into the timestamp type so all values sort and compare correctly. Check any strings in an unusual format, since they could have been parsed with the wrong timezone.
- **clubs.owner_uid**: Same uuid5 conversion as the other user ids, stored as owner_id so ownership-based policies can match auth.uid(). The same caveat applies: the auth side must produce identical uuids.
- **clubs.settings**: Kept as jsonb because the keys vary from document to document. Nothing in the database enforces its shape, so the application must validate keys it depends on.
- **courts.hourly_rate**: Numeric strings were converted to numbers, and one value was affected. Confirm that conversion is correct for that one row, since a wrong value would silently change pricing.
- **members.dupr_rating**: Two numeric strings were converted to numbers. One non-numeric value was set to NULL, which is data loss, so review that member's original value if the rating matters.
- **members.uid**: Firebase user id converted to a uuid user_id using uuid5 so row-level security can match auth.uid(). The conversion is one-way, so the mapping from Firebase uid to uuid must be kept consistent wherever users are created or migrated.
- **leagues.club_id**: Every value matches an existing club, so a foreign key was added with no rows removed. Leagues always belong to a valid club.
- **payments.club_id**: Every value matches an existing club, so a foreign key was added with no rows removed. Payments always link to a valid club.
- **payments.member_uid**: Converted to a uuid member_id with uuid5 so policies can compare it with auth.uid(). Confirm that the same derivation is used across all tables so one member's uuid matches everywhere.
- **payments.metadata**: Kept as jsonb because the keys differ between documents. Financial fields that matter for reporting should be promoted to real columns later rather than queried from this blob.
- **payments.method**: Most documents had the same keys, last4 and type, so they were flattened into columns. Documents missing those keys will have NULLs, so check that NULL is an acceptable meaning for them.
- **bookings.players**: This is an array of references, so it became a join table, bookings_players, which allows proper foreign keys and queries in both directions. Make sure every referenced player exists before the foreign keys are enforced.
- **members.favorite_courts**: This is an array of references, so it became the join table members_favorite_courts. That keeps each member-court pair as its own row with working foreign keys.

## Risks

- The payments:update and leagues:read rules are flagged and need a human to confirm their intended behavior before cutover.
- Three skipped records (bk_dup, bk_orphan, pay_orphan) mean data present in Firebase will not exist in the new database and must be accepted or resolved.
- Seven value fixes, including setting a non-numeric DUPR rating to NULL, change stored data and should be reviewed for business impact.

## Cutover checklist

1. Freeze writes on the Firebase clubs (deploy read-only rules) at the cutover time and announce a 30-minute window.
2. Re-export Firestore after the freeze and re-run this migration; the proof must pass again on the fresh export.
3. Import Firebase Auth users into Supabase Auth with the same uuid5 ids this migration used (24 distinct users referenced), so auth.uid() matches every migrated row.
4. Write the policy for flagged rule payments:update by hand (custom auth token claim `request.auth.token.admin` has no equivalent in the migrated schema). Until then that access is denied.
5. Write the policy for flagged rule leagues:read by hand (custom auth token claim `request.auth.token.email_verified` has no equivalent in the migrated schema). Until then that access is denied.
6. Update the app: array fields now live in join tables (bookings_players, members_favorite_courts), which are read-only to users until write policies are added.
7. Point the app's reads and writes at Supabase, keep Firebase read-only for 7 days as the rollback path.
8. Run the Supabase security and performance advisors on the new schema before opening traffic.

Migration SQL: `/Users/henrymoses/Desktop/buildwithmoses-automations/company-based-builds/supabase/supabase-agents/evals/firebase_migration/out/migration-20261029T144646-33e0.sql`
