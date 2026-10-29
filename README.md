# Supabase Agents: five agents where Postgres decides who sees what

Five LangGraph agents for **Courtline**, a fictional booking app for tennis and pickleball clubs that runs on
Supabase after starting on Firebase. They run against a **real local Supabase stack** (`supabase start`):
Postgres 17, pgvector, row-level security, Supabase's `anon`/`authenticated` roles and `auth.uid()`.

> "The model drafts and explains. Postgres enforces who sees what. Any change to the database's schema,
> indexes or policies, and any reply to a customer, waits for a named person."

The core idea: **agents read on a user's behalf as that user.** Every end-user query runs in a read-only,
rolled-back transaction that logs in as Supabase's `authenticator` role and switches to `authenticated` with the
user's JWT claims, exactly like a request through the Supabase API. So even a wrong query, a prompt injection or
a buggy filter can only see what that user is allowed to see.

> **Everything is fictional.** Courtline, its clubs, players, staff, help articles and Firestore export are made
> up ([`fixtures/COMPANY.md`](fixtures/COMPANY.md)). Embeddings are local (bge-small via fastembed), so no data
> leaves the machine except prompts to the model.

## The five agents

| Agent | What it does | What the gate blocked |
|---|---|---|
| [Docs Q&A](docs/docs-qa.md) | Hybrid pgvector + full-text search with reciprocal rank fusion in one SQL function; RLS hides staff-only articles from players; every citation and number verified in code | Searching as an admin: staff-only articles to players on 19 of 24 questions, while answer quality still scored perfectly |
| [RLS analyst](docs/rls-analyst.md) | Question to SQL, guarded with sqlglot, executed only as the asking user under RLS; tells "no access" apart from "no data" without returning hidden rows | Running as service role: a player got every member's email and phone number |
| [Database health](docs/db-health.md) | Splinter-style lints, pg_stat_statements and EXPLAIN find planted problems; fixes are generated as migrations and proven on a shadow copy before the owner approves | Naive fixes: the shadow proof caught all three wrong ones, including a policy that hid players' own bookings |
| [Support memory](docs/support-memory.md) | Per-customer long-term memory in Postgres with pgvector, behind RLS; code filters what gets remembered; "forget me" really deletes; money promises wait for approval | App-side filtering as service role: another member's notes leaked and forgotten memories came back |
| [Firebase migration](docs/firebase-migration.md) | Firestore export to tables, Firebase security rules to RLS policies, proven in a trial schema by re-running every rules test as the matching user | The model writing the policies: it passed the regular tests but guessed two rules with JWT claims Supabase doesn't carry |

## Results

All five ran together on one local Supabase on 2026-10-29. Every hard floor held at 1.0 and every gate passed;
each agent's regression versions were run separately and blocked.

| Agent | Cases | Hard floors (all 1.0) | Quality |
|---|---|---|---|
| Docs Q&A | 32 questions, 2 users | valid citations, no staff content to players, "don't know" when unanswerable, no deprecated answers, injection resisted | recall@5 1.0, correctness 1.0, p50 1.85 s |
| RLS analyst | 31 questions as 6 users + 18 SQL attacks | tenant isolation, executed as the asker, every bypass blocked, numbers match rows | answer accuracy 0.947 |
| Database health | 8 planted problems, 4 clean tables | all found, no false findings, every migration proven, approval, RLS correct after apply | calendar query 11–14x faster |
| Support memory | 25 multi-session scenarios | no cross-customer memory, forgotten stays forgotten, no secrets stored, no unapproved money promises, injection not stored | memory used when relevant 1.0 |
| Firebase migration | 106 docs, 6 collections, 35 rules tests | rules tests match Firebase, counts reconcile, referential integrity, unsupported rules flagged, approval | field fidelity 1.0 |

148 tests pass (offline unit tests, plus database tests that skip when Supabase isn't running). Total OpenRouter
spend for the whole build was under $1.

### Findings worth knowing

- **Quality scores don't see leaks.** The docs agent's admin-search regression scored perfect correctness while
  handing staff articles to players. The support-memory regression's helpfulness score passed its minimum while
  leaking other members' notes. Only checks run as the real user caught them.
- **Connecting as the superuser and "switching to the user" isn't enough.** The analyst build found that a
  query could `RESET ROLE` back to `postgres`. End-user work now logs in as `authenticator`, like PostgREST, and a
  test proves `SET ROLE postgres` is refused.
- **RLS changes how updates work.** A soft delete that sets `deleted_at` produces a row the read policy hides,
  so Postgres rejects it. Forget and supersede go through `SECURITY DEFINER` functions scoped to `auth.uid()`.
- **Prove migrations on a copy.** Every database-health fix is applied to a shadow schema first; it must clear
  its finding, add none, and keep each role's rows correct.

## Run it

```bash
supabase start -x realtime,storage-api,imgproxy,edge-runtime,logflare,vector,supavisor,mailpit
cp .env.example .env            # OPENROUTER_API_KEY; the local database URL is the default
uv sync
uv run pytest -q
uv run python evals/<agent>/run.py && uv run python evals/<agent>/gate.py
```

Each agent's doc has its setup command (seed or ingest), CLI and regression variants. To go live, point
`SUPABASE_DB_URL` (and `SUPABASE_USER_DB_URL` for the `authenticator` login) at a real project.

```
fixtures/                 the fictional company and the Firestore export
supabase/migrations/      each agent's schema, seed and policies (one schema per agent)
src/supabase_agents/core  config (OpenRouter models, local embeddings), db (user_tx as authenticator), cost, gate
src/supabase_agents/<agent>/   code rules, SQL, LangGraph graph with approval interrupts, cli
evals/<agent>/            run.py, gate.py, baseline.json, results/
```

Sibling builds: [posthog-agents](https://github.com/buildwithmoses/posthog-agents),
[elevenlabs-agents](https://github.com/buildwithmoses/elevenlabs-agents),
[n8n-agents](https://github.com/buildwithmoses/n8n-agents),
[langchain-agents](https://github.com/buildwithmoses/langchain-agents),
[openrouter-agents](https://github.com/buildwithmoses/openrouter-agents).
