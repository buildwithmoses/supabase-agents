-- Courtline support agent with per-customer memory (fictional data). Schema: support.
-- Who sees what is enforced here, by row-level security on auth.uid(), not in app code.
-- The agent reads and writes memories inside core.db.user_tx(customer_id): role `authenticated`,
-- request.jwt.claims.sub = the customer. A bug in the app can't widen what Postgres returns.

create extension if not exists vector with schema extensions;
create schema if not exists support;

create table if not exists support.clubs (
  id text primary key,
  name text not null,
  city text not null
);

create table if not exists support.customers (
  id uuid primary key,                       -- = auth.users.id / JWT sub
  club_id text not null references support.clubs(id),
  name text not null,
  email text not null unique,
  role text not null check (role in ('player', 'club_admin')),
  created_at timestamptz not null default now()
);

create table if not exists support.bookings (
  id bigserial primary key,
  customer_id uuid not null references support.customers(id),
  club_id text not null references support.clubs(id),
  court text not null,
  sport text not null check (sport in ('tennis', 'pickleball')),
  starts_at timestamptz not null,
  status text not null check (status in ('booked', 'cancelled', 'no_show', 'played'))
);

create table if not exists support.payments (
  id bigserial primary key,
  customer_id uuid not null references support.customers(id),
  kind text not null check (kind in ('membership', 'booking', 'late_cancel_fee', 'league_fee')),
  amount_cents int not null,
  status text not null check (status in ('paid', 'failed', 'refunded', 'pending')),
  created_at timestamptz not null
);

create table if not exists support.leagues (
  id bigserial primary key,
  club_id text not null references support.clubs(id),
  name text not null,
  night text not null,
  fee_cents int not null,
  spots_left int not null
);

create table if not exists support.conversations (
  id uuid primary key default gen_random_uuid(),
  customer_id uuid not null references support.customers(id),
  started_at timestamptz not null default now()
);

create table if not exists support.messages (
  id bigserial primary key,
  conversation_id uuid not null references support.conversations(id) on delete cascade,
  customer_id uuid not null references support.customers(id),
  role text not null check (role in ('customer', 'agent')),
  content text not null,
  created_at timestamptz not null default now()
);

create table if not exists support.memories (
  id bigserial primary key,
  customer_id uuid not null references support.customers(id),
  club_id text not null references support.clubs(id),   -- kept for reporting; never used to scope reads
  kind text not null check (kind in ('preference', 'fact', 'open_issue', 'commitment')),
  content text not null,
  embedding extensions.vector(384) not null,
  source_message_id bigint references support.messages(id) on delete set null,
  created_at timestamptz not null default now(),
  expires_at timestamptz,
  deleted_at timestamptz,
  superseded_by bigint references support.memories(id) on delete set null
);
create index if not exists memories_customer_idx on support.memories (customer_id) where deleted_at is null;

-- Money actions the agent may only request. Nadia approves or rejects; nothing pays out automatically.
create table if not exists support.approvals (
  id bigserial primary key,
  customer_id uuid not null references support.customers(id),
  conversation_id uuid references support.conversations(id) on delete set null,
  kind text not null check (kind in ('refund', 'credit', 'fee_waiver', 'money_reply')),
  amount_cents int,
  proposed_reply text,
  status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  decided_by text,
  decided_at timestamptz,
  created_at timestamptz not null default now()
);

-- ------------------------------------------------------------------ RLS
alter table support.clubs enable row level security;
alter table support.customers enable row level security;
alter table support.bookings enable row level security;
alter table support.payments enable row level security;
alter table support.leagues enable row level security;
alter table support.conversations enable row level security;
alter table support.messages enable row level security;
alter table support.memories enable row level security;
alter table support.approvals enable row level security;

-- The caller's club, without recursing through customers' own policy.
create or replace function support.my_club() returns text
language sql stable security definer set search_path = '' as $$
  select club_id from support.customers where id = (select auth.uid())
$$;
revoke all on function support.my_club() from public;
grant execute on function support.my_club() to authenticated;

drop policy if exists own_club on support.clubs;
create policy own_club on support.clubs for select to authenticated using (id = (select support.my_club()));
drop policy if exists self on support.customers;
create policy self on support.customers for select to authenticated using (id = (select auth.uid()));
drop policy if exists own_bookings on support.bookings;
create policy own_bookings on support.bookings for select to authenticated using (customer_id = (select auth.uid()));
drop policy if exists own_payments on support.payments;
create policy own_payments on support.payments for select to authenticated using (customer_id = (select auth.uid()));
drop policy if exists club_leagues on support.leagues;
create policy club_leagues on support.leagues for select to authenticated using (club_id = (select support.my_club()));

drop policy if exists own_conversations on support.conversations;
create policy own_conversations on support.conversations for all to authenticated
  using (customer_id = (select auth.uid())) with check (customer_id = (select auth.uid()));
drop policy if exists own_messages on support.messages;
create policy own_messages on support.messages for all to authenticated
  using (customer_id = (select auth.uid())) with check (customer_id = (select auth.uid()));

-- Memories: a session acting for a customer reads, writes and forgets only that customer's live memories.
drop policy if exists own_memories_read on support.memories;
create policy own_memories_read on support.memories for select to authenticated
  using (customer_id = (select auth.uid()) and deleted_at is null);
drop policy if exists own_memories_insert on support.memories;
create policy own_memories_insert on support.memories for insert to authenticated
  with check (customer_id = (select auth.uid()) and club_id = (select support.my_club()) and deleted_at is null);
drop policy if exists own_memories_update on support.memories;
create policy own_memories_update on support.memories for update to authenticated
  using (customer_id = (select auth.uid())) with check (customer_id = (select auth.uid()));
drop policy if exists own_memories_delete on support.memories;
create policy own_memories_delete on support.memories for delete to authenticated
  using (customer_id = (select auth.uid()));

drop policy if exists own_approvals on support.approvals;
create policy own_approvals on support.approvals for select to authenticated using (customer_id = (select auth.uid()));
drop policy if exists own_approvals_insert on support.approvals;
create policy own_approvals_insert on support.approvals for insert to authenticated
  with check (customer_id = (select auth.uid()) and status = 'pending');

grant usage on schema support to authenticated;
grant select on support.clubs, support.customers, support.bookings, support.payments, support.leagues to authenticated;
grant select, insert, update, delete on support.conversations, support.messages, support.memories to authenticated;
grant select, insert on support.approvals to authenticated;
grant usage on all sequences in schema support to authenticated;

-- Soft delete hides a memory from retrieval at once (the read policy filters deleted_at). The purge, run in the
-- same customer's session, then removes the rows for good: "forget" leaves nothing behind.
create or replace function support.forget_purge() returns int
language sql security invoker set search_path = '' as $$
  with gone as (delete from support.memories where customer_id = (select auth.uid()) and deleted_at is not null returning 1)
  select count(*)::int from gone
$$;
grant execute on function support.forget_purge() to authenticated;
