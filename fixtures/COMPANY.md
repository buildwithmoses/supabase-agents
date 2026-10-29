# Courtline (fictional)

Everything here is made up: the company, its clubs, players, staff and numbers. Domains end in `.example`.

Courtline is a booking app for **tennis and pickleball clubs**: members book courts, join leagues and pay
fees; club staff manage courts, lessons and memberships. About 340 clubs and 120,000 players. The app is
built on **Supabase** (Postgres, Auth, RLS) after starting on Firebase; a few clubs still run on the old
Firebase backend.

Multi-tenant model: every row belongs to a `club_id`. Players see their own bookings and their club's
public data; club staff see everything for their club; nobody sees another club's members or payments.
Row-level security enforces this in the database, not in app code.

## People (approvers)

- **Nadia Brooks** (@nadia) Head of Support. Owns customer replies and support memory.
- **Sam Torres** (@sam) Staff engineer, database owner. Approves every migration and index change.
- **Priya Shah** (@priya) Data lead. Owns analytics questions and what data leaves the database.

## Standing rule

**"The model drafts and explains. Postgres enforces who sees what. Any change to the database's schema,
indexes or policies, and any reply to a customer, waits for a named person."**
