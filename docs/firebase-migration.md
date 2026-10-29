# Firebase-to-Supabase migration assistant

Courtline (fictional) started on Firebase. Three clubs still run there. Moving them by hand means reading a
Firestore export, guessing a schema, rewriting the security rules as Postgres row-level security, and hoping
nobody can suddenly read another club's payments. This agent does the migration the way a database owner
would want it done: **code decides the schema, the policies and the data load; the migration is proven in a
throwaway schema; and nothing touches the real schema until @sam approves.**

> Fictional data: [`fixtures/firebase/`](../fixtures/firebase/) holds a generated Firestore export (3 clubs,
> 106 documents across 6 collections), the clubs' `firestore.rules`, 35 rules test cases with Firebase's
> expected allow/deny, and hand-written labels (`expected.json`).

## How it works

```
analyze (code)  -> review (model)  -> build (code)  -> prove (code, trial schema) -> report (model + code)
                -> approval (LangGraph interrupt, only @sam) -> apply (schema `migration`, only if proven)
```

| Step | Who | What |
|---|---|---|
| Analyze | code | Walks the export (subcollections included) and infers a relational schema: one table per collection with `_path` (PK) and `id`; parent key columns; timestamps; references as foreign keys; arrays of references as join tables; consistent maps flattened, free-form maps as `jsonb`; Firebase uids as `uuid` columns (uuid5 mapping) so `auth.uid()` can be compared to them; `*_id` text fields that match a collection's ids become foreign keys; nullability from presence. |
| Review | model (Claude Haiku 5.5) | Explains each decision in plain words and may suggest clearer table names. It can't change a type. Renames are applied only if they are valid, unique identifiers. |
| Build | code | Converts every document, logging each changed value (fix) and each document it can't load (skip) with a reason. Translates `firestore.rules` into RLS with a fixed pattern table (below). Writes one migration SQL file. |
| Prove | code | Runs that SQL in `migration_trial_<run>`, then checks row counts, referential integrity, field-by-field fidelity, and re-runs all 35 rules tests as the matching end user (role `authenticated`/`anon`, JWT claims set the way Supabase does). The trial schema is always dropped. |
| Report | model + code | The model writes a short summary from the facts; a code check rejects any number not in the facts and falls back to a template. Code adds the proof tables, skips, fixes, flagged rules and a cutover checklist. |
| Approval | @sam | LangGraph interrupt. A rejection, or an approval from anyone else, changes nothing. |
| Apply | code | Only if the proof passed and @sam approved: the same SQL that was proven runs into schema `migration`, with a `_migration_meta` row recording the run and approver. |

### Rule translation (code, not the model)

| Firestore pattern | RLS |
|---|---|
| `request.auth != null` | `auth.uid() is not null` |
| `request.auth.uid == resource.data.member_uid` | `auth.uid() = member_id` in `USING` |
| `request.auth.uid == request.resource.data.member_uid` | same, in `WITH CHECK` |
| `exists(/…/clubs/$(c)/members/$(request.auth.uid))` | `fb_exists_members_by_uid(c, auth.uid())`, a `SECURITY DEFINER` helper (no policy recursion) |
| `get(/…/members/$(request.auth.uid)).data.role in [...]` | `fb_get_members_role_by_uid(c, auth.uid()) = any(array[...])` |
| user-defined functions | inlined with their arguments |
| `resource.data` on create | Firestore errors, so the comparison is false |
| `if false` | no policy (deny) |
| custom token claims, `request.time`, `timestamp.*`, `.keys()`/`.hasOnly()`, old-vs-new comparisons | **flagged for a human, no policy: the table fails closed** |

On this export the translator wrote 16 policies plus 2 read policies for the join tables, and flagged 2 rules:
`payments:update` (custom claim `request.auth.token.admin`) and `leagues:read` (`email_verified` claim and
`request.time`).

### What the export threw at it

| Planted problem | What the agent did |
|---|---|
| Exact duplicate booking `bk_dup` | skipped, "duplicate of bookings/bk007" |
| Booking pointing at deleted court `c9` | skipped, orphan reference (court is required) |
| Payment from a deleted member | skipped, orphan reference |
| Ratings stored as `"4.5"`, `"3.75"` | converted to numbers, logged |
| Rating `"n/a"` | set to NULL, logged |
| Hourly rate `"30"` | converted, logged |
| Start time stored as an ISO string | parsed as a timestamp, logged |
| A deleted member inside a booking's `players` array | dropped from the join table, logged |
| `4000.0` in an integer field | stored as 4000, logged |

## Results

Live runs against the local Supabase stack (Postgres 17), real model calls, 2026-10-29. Each eval runs the
graph three times: rejected, "approved" by the wrong person, approved by @sam.

| Metric | Floor | Baseline | Baseline (repeat) | Model writes the RLS | No trial proof |
|---|---|---|---|---|---|
| Rules tests match Firebase (33 regular tests) | 1.0 | 1.0 | 1.0 | 1.0 | not measured |
| Row counts reconcile, every skip/fix explained and matching the labels | 1.0 | 1.0 | 1.0 | 1.0 | not measured |
| Referential integrity (0 orphans, 11 foreign keys) | 1.0 | 1.0 | 1.0 | 1.0 | not measured |
| Unsupported rules flagged and denied, not guessed | 1.0 | 1.0 | 1.0 | **0.0** | not measured |
| Nothing applied without @sam, applied schema matches the proof | 1.0 | 1.0 | 1.0 | 1.0 | **0.0** |
| Field fidelity (819 fields read back from Postgres) | 0.99 | 1.0 | 1.0 | 1.0 | not measured |
| Schema decisions match the labels (jsonb, flattening, join tables) | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| Report quality (GPT-6.1 Terra judge) | 0.7 | 0.93 | 0.91 | 0.957 | 0.893 |
| Gate | | **passed** | **passed** | **blocked** | **blocked** |

**What the regressions did.**

- **The model writing RLS got every ordinary rule right and still failed the one floor that matters.** Its
  policies matched Firebase on all 33 regular tests. But it listed no rule as untranslatable and guessed both
  hard ones: `payments` refunds became `(auth.jwt() ->> 'admin') = 'true'` and the league page became
  `(auth.jwt() ->> 'email_verified') = 'true' and now() < '2026-12-31'`. Supabase JWTs don't carry either claim
  at the top level by default, so in production these would silently do the wrong thing, and no one would
  have been told. The report judge rated this version's report *higher* (0.957). That is why "flagged, not
  guessed" is a hard floor in code, not a judge score.
- **Skipping the trial proof** let @sam's approval apply SQL nobody had checked. The gate fails it on every
  proof floor (missing) and on `approval_respected` (applied without a passing proof).

**Found while building it.**

- The model's first output for the RLS regression was cut off at 4,000 tokens, so it "failed" for the wrong
  reason. The regression now gets 12,000 tokens and a separate field for helper functions, so it's a fair
  comparison.
- Postgres folds unquoted names to lowercase: a trial schema named `migration_trial_20261029T1...` was
  created as `...t1...`, and the foreign-key count looked in the wrong schema and reported 0. Trial names are
  now lowercased.
- The report judge first saw only the summary facts and marked true statements (the uuid5 mapping, the join
  tables) as unsupported. It now gets everything the report is built from.
- RLS differs from Firestore in one way worth knowing at cutover: an `UPDATE` only sees rows the user can
  `SELECT`. Firestore evaluates `update` on its own. Every update rule here is held by someone who can also
  read the row, so the tests match, but a rule like "the billing service can refund but not read" needs a
  read policy too.

**Cost.** About $0.004 of Haiku 5.5 for three full agent runs and $0.007 for the judge per eval. Everything
built and run for this agent, regressions included, came to under $0.10. A full run takes 11 to 15 seconds.

## Run it

```bash
supabase start                                                       # local stack (Postgres on :54322)
uv run python -m supabase_agents.firebase_migration.fixture_gen      # regenerate the fictional export
uv run python -m supabase_agents.firebase_migration.cli plan         # analyze, prove, report; stops for approval
uv run python -m supabase_agents.firebase_migration.cli plan --approve sam
uv run python -m supabase_agents.firebase_migration.cli plan --export my-export.json --rules firestore.rules --tests tests.json
uv run python evals/firebase_migration/run.py [--variant model-rls|no-proof]
uv run python evals/firebase_migration/gate.py [--variant model-rls|no-proof]
uv run pytest -q tests/test_firebase_migration.py                    # 19 offline tests, no database or keys
```

Each run writes `evals/firebase_migration/out/migration-<run>.sql` (the exact SQL that was proven) and, from
the CLI, `report-<run>.md`.

## Limits

- Collections nested more than one level deep are flagged (parent keys use one level).
- Join tables get read policies only; array edits need write policies and an app change (on the checklist).
- Firebase Auth users must be imported into Supabase Auth with the same uuid5 ids (on the checklist).
- The approver identity is a name passed on resume; production should take it from Slack or SSO.
