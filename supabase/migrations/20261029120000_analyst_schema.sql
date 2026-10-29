-- Courtline analyst schema (fictional data). Multi-tenant by club_id, enforced by row-level security.
-- Who sees what:
--   clubs                     every signed-in user (names and cities only)
--   courts, leagues,
--   league_entries, profiles  members of that club
--   memberships, bookings,
--   payments                  your own rows, or every row of a club where you are staff
--   contacts (email, phone)   your own row, or staff of that club
-- Nobody sees another club's members, bookings, payments or contact details.

drop schema if exists analyst cascade;
create schema analyst;

create table analyst.clubs (
  id int primary key,
  name text not null,
  city text not null,
  sport_focus text not null
);

create table analyst.profiles (
  user_id uuid primary key,
  display_name text not null
);

create table analyst.memberships (
  user_id uuid not null references analyst.profiles(user_id),
  club_id int not null references analyst.clubs(id),
  role text not null check (role in ('player', 'staff')),
  tier text not null check (tier in ('basic', 'premium', 'junior')),
  joined_on date not null,
  primary key (user_id, club_id)
);

create table analyst.contacts (
  user_id uuid not null references analyst.profiles(user_id),
  club_id int not null references analyst.clubs(id),
  email text not null,
  phone text not null,
  primary key (user_id, club_id)
);

create table analyst.courts (
  id int primary key,
  club_id int not null references analyst.clubs(id),
  name text not null,
  sport text not null check (sport in ('tennis', 'pickleball')),
  surface text not null,
  indoor boolean not null
);

create table analyst.bookings (
  id int primary key,
  club_id int not null references analyst.clubs(id),
  court_id int not null references analyst.courts(id),
  user_id uuid not null references analyst.profiles(user_id),
  starts_at timestamptz not null,
  duration_min int not null,
  price_cents int not null,
  status text not null check (status in ('confirmed', 'cancelled', 'no_show'))
);

create table analyst.payments (
  id int primary key,
  club_id int not null references analyst.clubs(id),
  user_id uuid not null references analyst.profiles(user_id),
  booking_id int references analyst.bookings(id),
  kind text not null check (kind in ('booking', 'membership', 'league')),
  amount_cents int not null,
  paid_at timestamptz not null
);

create table analyst.leagues (
  id int primary key,
  club_id int not null references analyst.clubs(id),
  name text not null,
  sport text not null,
  season text not null
);

create table analyst.league_entries (
  league_id int not null references analyst.leagues(id),
  user_id uuid not null references analyst.profiles(user_id),
  club_id int not null references analyst.clubs(id),
  wins int not null default 0,
  losses int not null default 0,
  primary key (league_id, user_id)
);

create index on analyst.memberships (club_id);
create index on analyst.bookings (club_id, starts_at);
create index on analyst.bookings (user_id);
create index on analyst.payments (club_id, paid_at);
create index on analyst.payments (user_id);

-- Membership lookups for policies. SECURITY DEFINER so a policy can read memberships without recursing
-- into memberships' own policy; empty search_path so nothing can be shadowed.
create or replace function analyst.my_club_ids() returns setof int
language sql stable security definer set search_path = '' as $$
  select m.club_id from analyst.memberships m where m.user_id = (select auth.uid())
$$;

create or replace function analyst.my_staff_club_ids() returns setof int
language sql stable security definer set search_path = '' as $$
  select m.club_id from analyst.memberships m where m.user_id = (select auth.uid()) and m.role = 'staff'
$$;

revoke all on function analyst.my_club_ids() from public, anon;
revoke all on function analyst.my_staff_club_ids() from public, anon;
grant execute on function analyst.my_club_ids() to authenticated;
grant execute on function analyst.my_staff_club_ids() to authenticated;

alter table analyst.clubs enable row level security;
alter table analyst.profiles enable row level security;
alter table analyst.memberships enable row level security;
alter table analyst.contacts enable row level security;
alter table analyst.courts enable row level security;
alter table analyst.bookings enable row level security;
alter table analyst.payments enable row level security;
alter table analyst.leagues enable row level security;
alter table analyst.league_entries enable row level security;

create policy clubs_read on analyst.clubs for select to authenticated using (true);

create policy profiles_read on analyst.profiles for select to authenticated using (
  user_id = (select auth.uid())
  or exists (select 1 from analyst.memberships m
             where m.user_id = profiles.user_id and m.club_id in (select analyst.my_club_ids()))
);

create policy memberships_read on analyst.memberships for select to authenticated using (
  user_id = (select auth.uid()) or club_id in (select analyst.my_staff_club_ids())
);

create policy contacts_read on analyst.contacts for select to authenticated using (
  user_id = (select auth.uid()) or club_id in (select analyst.my_staff_club_ids())
);

create policy courts_read on analyst.courts for select to authenticated using (club_id in (select analyst.my_club_ids()));
create policy leagues_read on analyst.leagues for select to authenticated using (club_id in (select analyst.my_club_ids()));
create policy league_entries_read on analyst.league_entries for select to authenticated using (club_id in (select analyst.my_club_ids()));

create policy bookings_read on analyst.bookings for select to authenticated using (
  user_id = (select auth.uid()) or club_id in (select analyst.my_staff_club_ids())
);

create policy payments_read on analyst.payments for select to authenticated using (
  user_id = (select auth.uid()) or club_id in (select analyst.my_staff_club_ids())
);

-- Read-only for signed-in users; anon gets nothing. Writes only through the app's own paths (not this agent).
grant usage on schema analyst to authenticated;
grant select on all tables in schema analyst to authenticated;
revoke all on all tables in schema analyst from anon;
