-- Courtline schema for the db-health agent (schema `health`). Fictional data, seeded by
-- src/supabase_agents/db_health/seed.py. Problems are planted ON PURPOSE so the agent has something to find:
--   P1 leagues: RLS disabled on an API-exposed table
--   P2 lesson_notes: RLS enabled but no policy (staff can't see their own notes)
--   P3 bookings: policy calls auth.uid() per row instead of (select auth.uid())
--   P4 bookings: three foreign keys without an index (club_id, court_id, member_id)
--   P5 members: duplicate index on user_id
--   P6 bookings: hot query filters club_id + start_time with no index (sequential scan)
--   P7 is_staff(): SECURITY DEFINER function with a mutable search_path
--   P8 payments: SELECT policy USING (true) lets every signed-in user read every club's payments
-- clubs, courts, coaches and announcements are clean controls.

create schema if not exists health;
grant usage on schema health to authenticated, anon;

create table health.clubs (
  id uuid primary key,
  name text not null,
  city text not null,
  created_at timestamptz not null default now()
);

create table health.members (
  id uuid primary key,
  club_id uuid not null references health.clubs (id),
  user_id uuid not null,
  role text not null check (role in ('player', 'staff')),
  display_name text not null,
  created_at timestamptz not null default now()
);
create index members_club_id_idx on health.members (club_id);
create index members_user_id_idx on health.members (user_id);
create index members_user_idx on health.members (user_id);            -- P5 duplicate

create table health.courts (
  id uuid primary key,
  club_id uuid not null references health.clubs (id),
  name text not null,
  surface text not null
);
create index courts_club_id_idx on health.courts (club_id);

create table health.coaches (
  id uuid primary key,
  club_id uuid not null references health.clubs (id),
  name text not null
);
create index coaches_club_id_idx on health.coaches (club_id);

create table health.announcements (
  id bigint generated always as identity primary key,
  club_id uuid not null references health.clubs (id),
  title text not null,
  body text not null,
  posted_at timestamptz not null default now()
);
create index announcements_club_id_idx on health.announcements (club_id);

create table health.bookings (                                          -- P4, P6: no indexes beyond the key
  id bigint generated always as identity primary key,
  club_id uuid not null references health.clubs (id),
  court_id uuid not null references health.courts (id),
  member_id uuid not null references health.members (id),
  start_time timestamptz not null,
  end_time timestamptz not null,
  status text not null default 'confirmed'
);

create table health.payments (
  id bigint generated always as identity primary key,
  club_id uuid not null references health.clubs (id),
  member_id uuid not null references health.members (id),
  amount_cents int not null,
  status text not null,
  created_at timestamptz not null
);
create index payments_club_id_idx on health.payments (club_id);
create index payments_member_id_idx on health.payments (member_id);

create table health.leagues (
  id uuid primary key,
  club_id uuid not null references health.clubs (id),
  name text not null,
  level text not null
);
create index leagues_club_id_idx on health.leagues (club_id);

create table health.lesson_notes (
  id bigint generated always as identity primary key,
  club_id uuid not null references health.clubs (id),
  member_id uuid not null references health.members (id),
  coach_id uuid not null references health.coaches (id),
  note text not null,
  created_at timestamptz not null default now()
);
create index lesson_notes_club_id_idx on health.lesson_notes (club_id);
create index lesson_notes_member_id_idx on health.lesson_notes (member_id);
create index lesson_notes_coach_id_idx on health.lesson_notes (coach_id);

-- Helpers used by policies. is_member is written correctly; is_staff is P7.
create function health.is_member(p_club uuid) returns boolean
language sql stable security definer set search_path = ''
as $$
  select exists (select 1 from health.members m where m.club_id = p_club and m.user_id = (select auth.uid()))
$$;

create function health.is_staff(p_club uuid) returns boolean
language sql stable security definer
as $$
  select exists (select 1 from health.members m where m.club_id = p_club and m.user_id = auth.uid() and m.role = 'staff')
$$;

alter table health.clubs enable row level security;
create policy clubs_directory on health.clubs for select to authenticated using (true);     -- public directory, intended

alter table health.members enable row level security;
create policy members_self on health.members for select to authenticated using (user_id = (select auth.uid()));

alter table health.courts enable row level security;
create policy courts_club_members on health.courts for select to authenticated using (health.is_member(club_id));

alter table health.coaches enable row level security;
create policy coaches_club_members on health.coaches for select to authenticated using (health.is_member(club_id));

alter table health.announcements enable row level security;
create policy announcements_club_members on health.announcements for select to authenticated using (health.is_member(club_id));

alter table health.bookings enable row level security;
create policy bookings_own on health.bookings for select to authenticated                    -- P3
  using (member_id in (select m.id from health.members m where m.user_id = auth.uid()));

alter table health.payments enable row level security;
create policy payments_all on health.payments for select to authenticated using (true);     -- P8

-- P1: health.leagues has no RLS at all.

alter table health.lesson_notes enable row level security;                                  -- P2: and no policy

grant select on all tables in schema health to authenticated;
