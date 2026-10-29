# Firestore -> Supabase migration report (20261029T141942-4685)

The migration loaded 103 rows across 6 tables, including 2 join tables and 16 policies. The proof passed: all 33 regular rules tests matched Firebase, and both flagged rule tests were denied as intended. Fidelity is 1.0 across 819 fields checked, with 0 orphans. Three rows were skipped: one duplicate (bk_dup) and two orphan references (bk_orphan, pay_orphan). Seven values were fixed, including timestamp parsing, dropped missing references, numeric string conversions, one non-numeric DUPR rating set to NULL, and one integral float stored as an integer. Two rules need human review before approval: payments:update and leagues:read.

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

- **bookings.club_id**: Every value points to an existing club, so a foreign key to clubs was added safely. Bookings cannot reference a club that does not exist.
- **bookings.member_uid**: The Firebase user id became a uuid column named member_id, built with uuid5 from the Firebase uid. This lets row-level security compare it to auth.uid(), but it only matches real logins if those auth users were created with the same uuid5 mapping.
- **bookings.starts_at**: Some values were Firestore timestamps and others were ISO strings, and the strings were parsed into one timestamp type. Check that any string without a timezone was read in the timezone you expect.
- **clubs.owner_uid**: The Firebase owner id became a uuid column named owner_id, using the same uuid5 mapping as the other user ids. Ownership checks in row-level security will work only once auth accounts use that mapping.
- **clubs.settings**: Settings keys vary from document to document, so the column is jsonb to hold whatever shape each club has. Nothing is enforced per key, so validate values in the application.
- **courts.hourly_rate**: Most rates were numbers, and one numeric string was converted. Confirm the converted value is in the same currency unit as the rest.
- **members.dupr_rating**: Two numeric strings were converted, but one non-numeric value was set to NULL, which loses that original value. Keep a copy of the source document if that rating must be recoverable.
- **members.uid**: The Firebase user id became a uuid column named user_id, built with uuid5 for row-level security. As with the other user ids, it matches auth.uid() only if auth accounts use the same mapping.
- **leagues.club_id**: Every value matches a club, so the foreign key was added without issues. Leagues always belong to an existing club.
- **payments.club_id**: Every value matches a club, so the foreign key is safe. Payments are tied to valid clubs.
- **payments.member_uid**: The Firebase user id became a uuid column named member_id using uuid5, so row-level security can check it against auth.uid(). The same mapping must be used when creating auth accounts for this to work.
- **payments.metadata**: Metadata keys differ across payments, so it is stored as jsonb. Expect no database-level checks on its contents.
- **payments.method**: The method map had the keys last4 and type in at least 90 percent of documents, so they became separate columns. Payments missing those keys will have NULLs in those columns, which is expected but worth checking.
- **bookings.players**: This array of references was moved to a join table, bookings_players, which is the standard way to store many-to-many links in Postgres. Foreign keys there keep player links consistent.
- **members.favorite_courts**: This array of references was moved to a join table, members_favorite_courts, so each favorite is a proper row that can be queried and constrained. The link can be enforced with foreign keys to members and courts.

## Risks

- Two flagged rules, payments:update and leagues:read, require human sign-off because they cannot be fully validated automatically.
- Three skipped rows (bk_dup, bk_orphan, pay_orphan) mean some data was not carried over and may need manual review.
- One non-numeric DUPR rating was set to NULL, so that member's rating data is lost in the target.

## Cutover checklist

1. Freeze writes on the Firebase clubs (deploy read-only rules) at the cutover time and announce a 30-minute window.
2. Re-export Firestore after the freeze and re-run this migration; the proof must pass again on the fresh export.
3. Import Firebase Auth users into Supabase Auth with the same uuid5 ids this migration used (24 distinct users referenced), so auth.uid() matches every migrated row.
4. Write the policy for flagged rule payments:update by hand (custom auth token claim `request.auth.token.admin` has no equivalent in the migrated schema). Until then that access is denied.
5. Write the policy for flagged rule leagues:read by hand (custom auth token claim `request.auth.token.email_verified` has no equivalent in the migrated schema). Until then that access is denied.
6. Update the app: array fields now live in join tables (bookings_players, members_favorite_courts), which are read-only to users until write policies are added.
7. Point the app's reads and writes at Supabase, keep Firebase read-only for 7 days as the rollback path.
8. Run the Supabase security and performance advisors on the new schema before opening traffic.

Migration SQL: `/Users/henrymoses/Desktop/buildwithmoses-automations/company-based-builds/supabase/supabase-agents/evals/firebase_migration/out/migration-20261029T141942-4685.sql`
