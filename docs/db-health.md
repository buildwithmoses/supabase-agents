# Database health agent (db-health)

Courtline (fictional) runs its booking app on Supabase. Sam Torres, the staff engineer who owns the database,
reviews the Supabase advisors when he remembers to, and every fix is hand-written SQL applied straight to
production. This agent does the review every time and writes the fixes. It proves each fix on a copy of the
schema first, and nothing touches the real schema until Sam approves it.

> Fictional data. Courtline, its clubs, members, bookings and payments are made up. Everything runs on the
> local Supabase stack (`supabase start`), schema `health`.

## The planted problems

The seed migration ([`supabase/migrations/20261029100000_health_seed_schema.sql`](../supabase/migrations/20261029100000_health_seed_schema.sql))
builds a realistic Courtline schema with 200,000 bookings and plants eight problems (ten findings). Four tables
are clean controls.

| # | Problem | Why it matters |
|---|---|---|
| P1 | `leagues` has RLS disabled but is readable through the API | every signed-in user sees every club's leagues |
| P2 | `lesson_notes` has RLS on and no policy | club staff can't read their own lesson notes |
| P3 | `bookings_own` policy calls `auth.uid()` per row | slower reads as bookings grow |
| P4 | three foreign keys on `bookings` without an index | slow deletes of clubs/courts/members and slow joins |
| P5 | two identical indexes on `members(user_id)` | wasted space and write work |
| P6 | the club calendar query reads all 200,000 bookings | 6–7 ms per call, 120 calls in the load test |
| P7 | `is_staff()` is SECURITY DEFINER with no fixed search_path | a hijackable name lookup in a privileged function |
| P8 | `payments_all` is `USING (true)` | every signed-in user can read every club's payments |
| — | `clubs`, `courts`, `coaches`, `announcements` | clean: any finding here is a false alarm |

## How it works

```
gather   code: lints (SQL, modeled on Supabase's Splinter checks) + pg_stat_statements + EXPLAIN
explain  model: Claude Haiku 5.5 explains each finding in plain English and drafts a fix
plan     code: writes each fix as migration SQL from a template (the model's draft is not applied)
prove    code: clones `health` into `health_shadow_<n>`, applies each migration in order, and checks it
approve  LangGraph interrupt: only @sam can approve; anyone else's answer is recorded and ignored
apply    code: applies exactly what was approved (re-proven first if only a subset was approved),
         writes each migration to supabase/migrations/, and re-lints the real schema
```

**Lints** (`lints.py`): `unindexed_foreign_keys`, `auth_rls_initplan`, `no_primary_key`,
`multiple_permissive_policies`, `policy_exists_rls_disabled`, `rls_enabled_no_policy`, `duplicate_index`,
`security_definer_view`, `function_search_path_mutable` and `rls_disabled_in_exposed_schema` (Splinter's
`rls_disabled_in_public`, applied to a schema granted to API roles). They're written in the style of Supabase's
Splinter lints with the same names, not copied from it. One custom lint, `permissive_policy`, flags a
`USING (true)` policy only when Courtline's access rule for that table isn't "public". That's why the public
`clubs` directory isn't flagged. `unused_index` isn't implemented: on a freshly seeded database every index
looks unused.

**Slow queries** (`perf.py`): a parameterized query on the schema with at least 50 calls and a mean of at least
2 ms is flagged if its generic plan (`EXPLAIN (GENERIC_PLAN)`) sequentially scans a table of at least 10,000
rows with a filter. The index columns come from the WHERE clause, parsed with sqlglot: equality columns first,
then one range column.

**Fixes** (`fixes.py`) are templates. Index builds use `CREATE INDEX CONCURRENTLY`, so the live bookings table
isn't locked. One index can fix two findings: `(club_id, start_time)` for the calendar query also covers the
`club_id` foreign key, so no separate `club_id` index is built. Access fixes use Courtline's written access rules
([`spec.py`](../src/supabase_agents/db_health/spec.py)).

**The shadow proof** (`shadow.py`) copies tables with their original index and constraint names, plus data,
foreign keys, functions, RLS flags, policies and grants. It first checks that the copy has exactly the same
findings as the real schema. Then, for each migration in order:

- it applies without error
- the findings it targets are gone, and no new finding appears
- **RLS:** five probe users (club 1 staff, a club 1 player, a club 2 player, a two-club user, an outsider) query
  every table as `authenticated` with their JWT claims. Tables the migration is allowed to change must then
  show each user exactly the rows Courtline's rules allow. Every other table must show the same rows as before.
- a performance migration must turn the calendar query's sequential scan into an index scan and run at least
  twice as fast, measured with `EXPLAIN ANALYZE`, best of 5

The copy is dropped afterwards. All nine migrations are proven in about 3.5 seconds.

## Results

Measured on 2026-10-29 against the local stack, with a full reseed and query load before every run.
Approvals in each run: @nadia approves everything, which must be ignored. Then @sam approves everything
except the duplicate-index drop, which must not be applied.

| Metric | Floor | Baseline run 5 | Baseline run 6 | model-writes | naive-fixes |
|---|---|---|---|---|---|
| Planted problems found (10) | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| No findings on clean tables | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| Every migration proven in a shadow schema | 1.0 | 1.0 | 1.0 | **0.0** | **0.667** |
| No change without @sam's approval | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| RLS: every table shows exactly the allowed rows after apply | 1.0 | 1.0 | 1.0 | 1.0 | **0.778** |
| Approved findings fixed in the real schema | min 1.0 | 1.0 | 1.0 | 1.0 | 1.0 |
| Calendar query speedup (real schema) | min 5x | 13.5x | 14.4x | 9.2x | 14.3x |
| Explanation clarity (GPT-6.1 Terra judge) | min 0.7 | 0.7 | 0.8 | 0.7 | not run |
| Gate | | PASSED | PASSED | **FAILED** | **FAILED** |

The calendar query went from about 6 ms to about 0.45 ms, a sequential scan replaced by a bitmap index scan.

**model-writes** applies the model's own draft SQL with no shadow proof. In this run Haiku's drafts were all
correct: 10 of 10 would have passed the proof, and the real schema ended up right. The gate still blocks it,
because nobody could have known that before applying. That is the process floor: an unproven migration does not
ship, however good it looks.

**naive-fixes** shows the proof catching real damage. It uses the baseline pipeline with three plausible-looking
wrong fixes:

- **bookings:** `member_id = (select auth.uid())`. The proof found that a player's own bookings vanish, so RLS
  changed on a table it must not change.
- **payments:** "anyone in the club" with a bare `auth.uid()`. The proof found a new `auth_rls_initplan` finding,
  and players would see their whole club's payments.
- **leagues:** RLS turned on without a policy. The proof found a new `rls_enabled_no_policy` finding, and members
  lose access to leagues.

All three were refused at approval even though @sam approved them, so they never touched `health`. The planted
leagues and payments exposures stayed unfixed, which is why RLS correctness is 0.778.

| | |
|---|---|
| Offline tests | 11 (`tests/test_db_health.py`), no database, model or keys |
| Cost per eval run | about $0.004 for the agent (10 Haiku calls) and about $0.013 for the judge |
| Wall time per eval run | 45–70 s, including two reseeds and a 200,000-row load |
| OpenRouter spend, all eval runs | $0.107 |

### What broke while building it

- **Clone faithfulness.** The duplicate-index finding id listed index names in creation order, and the clone
  created them in a different order. So the copy had a "different" finding and every proof failed. The ids now
  sort names, and the clone recreates indexes in their original order.
- **Judge input.** The judge first saw only the one-line finding, not the policies, function body and access rule
  the writer saw, so it called true statements "invented" and scored 0.2. It now gets the same facts.
- **Judge schema.** The judge model's provider rejected a Pydantic schema with nested `$defs`. A flat JSON
  schema works.
- **Writer accuracy.** The writer overstated Postgres behavior. It said every update maintains both duplicate
  indexes, and that any club update scans bookings, when only deletes and key changes do. The prompt now states
  the precise rules. The judge rubric was also calibrated: a minor overstatement that wouldn't change the
  approver's decision scores 0.5, and 0 is kept for anything that would mislead. Clarity went from 0.5 to 0.7–0.8.
  It sits close to the 0.7 minimum, and the remaining half-points are mostly foreign-key wording.

## Run it

```bash
supabase start                                              # local stack
uv run python -m supabase_agents.db_health.cli seed         # schema `health`, data, query load
uv run python -m supabase_agents.db_health.cli lint         # findings only, no model
uv run python -m supabase_agents.db_health.cli run          # explain, prove, then ask for @sam's approval
uv run python evals/db_health/run.py                        # baseline eval
uv run python evals/db_health/run.py --variant model-writes
uv run python evals/db_health/run.py --variant naive-fixes
uv run python evals/db_health/gate.py [--variant naive-fixes] [--save-baseline]
uv run pytest -q tests/test_db_health.py
```

`cli run` writes approved migrations to `supabase/migrations/`. Evals write them to a temp folder and reseed
`health` at the end.

## Limits

- **Approver identity.** The approver is a typed name. In production it should come from Slack or Supabase Auth.
- **Lints.** They are written in Splinter's style, not imported from it. Before trusting a production run,
  compare against the real Splinter or the Supabase dashboard advisors.
- **Concurrent index builds.** `CREATE INDEX CONCURRENTLY` must run outside a transaction. The applier does that,
  and the migration files say so in a comment.
- **Shadow copy size.** The proof copies the whole schema, which takes about 1 s here. A large production schema
  would need a sampled copy or a Supabase branch.
