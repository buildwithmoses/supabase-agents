# Firestore -> Supabase migration report (20261029T141717-6b89)

The migration loaded 103 rows and skipped 3: one duplicate (bookings/bk_dup, identical to bk007) and two orphan references (bookings/bk_orphan and payments/pay_orphan). Seven values were fixed, including timestamp parsing, dropping a missing player reference, converting numeric strings, setting one non-numeric DUPR rating to NULL, and storing an integral float as an integer. Across 6 tables, 2 join tables and 20 policies, 33 regular rules tests matched Firebase and 819 fields were checked with fidelity 1.0 and 0 orphans. The proof did not pass: 2 flagged rule tests were run and 0 were denied as intended. The flagged rules list is empty in the facts, so the 2 flagged tests need human review before approval.

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
- Rules tests: 1.0 match Firebase on 33 tests; flagged rules fail closed: 0.0

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

- none

## Decisions explained

- **bookings.club_id**: Every value already matches a club, so a foreign key to clubs was added safely. Bookings cannot point at a club that does not exist.
- **bookings.member_uid**: The Firebase user id became a uuid column named member_id, built deterministically with uuid5 so RLS can compare it to auth.uid(). The Supabase auth user ids must be generated the same way, or the policies will not match.
- **bookings.starts_at**: Some values were Firestore timestamps and others were ISO strings. Parsing the strings unifies the column as a timestamp, but check that the time zone of the ISO strings was interpreted correctly.
- **clubs.owner_uid**: Same approach as the member ids: the Firebase owner id became owner_id, a uuid5 value, so ownership-based RLS can work. It depends on the same auth id mapping being used everywhere.
- **clubs.settings**: The keys vary between documents, so storing it as jsonb keeps all data without forcing a rigid column list. Individual settings cannot be constrained or indexed by column this way.
- **courts.hourly_rate**: Most values were numbers and one numeric string was converted, so no data is lost. Still, confirm that the rate is in the currency and unit expected.
- **members.dupr_rating**: Two numeric strings were converted, but one non-numeric value became NULL. That value is lost from the table, so review the original document if it matters.
- **members.uid**: The Firebase user id became user_id, a uuid5 value, to support RLS checks against auth.uid(). Like the other ids, it only works if the auth accounts are created with matching ids.
- **leagues.club_id**: Every value matches a club, so the foreign key is safe and prevents orphaned leagues.
- **payments.club_id**: Every value matches a club, so the foreign key is safe and keeps payments tied to real clubs.
- **payments.member_uid**: The Firebase user id became member_id, a uuid5 value, to allow RLS to compare it to auth.uid(). It needs the same auth id mapping as the other member references.
- **payments.metadata**: The keys differ across documents, so jsonb preserves everything. Any metadata key you need to query often should be promoted to its own column later.
- **payments.method**: The map keys last4 and type appear in at least 90% of documents, so they became columns. Documents without them will have NULLs, and any other keys in the remaining documents need a check so nothing is silently dropped.
- **bookings.players**: Arrays of references were moved into the join table bookings_players, which is the normal relational form. Each player in a booking becomes one row, and it can be queried and constrained.
- **members.favorite_courts**: The array of court references moved to the join table members_favorite_courts, one row per member and court pair. This allows foreign keys to courts and makes the relation easy to query.

## Risks

- The proof failed, so the migration should not be approved for cutover until the flagged rule tests are resolved.
- Two flagged rule tests were not denied as intended, which may mean access rules behave differently after migration.
- Three skipped records (one duplicate and two orphan references) will be absent from the new database and may need manual confirmation.
- Seven value fixes, including setting a non-numeric DUPR rating to NULL, change stored data and should be reviewed by the owner.

## Cutover checklist

1. Freeze writes on the Firebase clubs (deploy read-only rules) at the cutover time and announce a 30-minute window.
2. Re-export Firestore after the freeze and re-run this migration; the proof must pass again on the fresh export.
3. Import Firebase Auth users into Supabase Auth with the same uuid5 ids this migration used (24 distinct users referenced), so auth.uid() matches every migrated row.
4. Update the app: array fields now live in join tables (bookings_players, members_favorite_courts), which are read-only to users until write policies are added.
5. Point the app's reads and writes at Supabase, keep Firebase read-only for 7 days as the rollback path.
6. Run the Supabase security and performance advisors on the new schema before opening traffic.

Migration SQL: `/Users/henrymoses/Desktop/buildwithmoses-automations/company-based-builds/supabase/supabase-agents/evals/firebase_migration/out/migration-20261029T141717-6b89.sql`
