-- Soft delete under RLS: an UPDATE that sets deleted_at produces a row the read policy (deleted_at is null)
-- no longer allows, so Postgres rejects it (and a DELETE can't see already-hidden rows). These functions run as
-- the owner but scope every statement to the caller's own auth.uid(), so a customer still only touches
-- their own memories, and nothing here takes a customer id from the caller.

create or replace function support.forget_memories(ids bigint[] default null)
returns table (id bigint, content text)
language sql security definer set search_path = '' as $$
  update support.memories m set deleted_at = now()
  where m.customer_id = (select auth.uid()) and m.deleted_at is null and (ids is null or m.id = any(ids))
  returning m.id, m.content
$$;

create or replace function support.supersede_memory(old_id bigint, new_id bigint)
returns int
language sql security definer set search_path = '' as $$
  with s as (
    update support.memories m set deleted_at = now(), superseded_by = new_id
    where m.id = old_id and m.customer_id = (select auth.uid()) and m.deleted_at is null
      and exists (select 1 from support.memories n where n.id = new_id and n.customer_id = (select auth.uid()))
    returning 1)
  select count(*)::int from s
$$;

create or replace function support.forget_purge() returns int
language sql security definer set search_path = '' as $$
  with gone as (delete from support.memories where customer_id = (select auth.uid()) and deleted_at is not null returning 1)
  select count(*)::int from gone
$$;

revoke all on function support.forget_memories(bigint[]), support.supersede_memory(bigint, bigint), support.forget_purge() from public;
grant execute on function support.forget_memories(bigint[]), support.supersede_memory(bigint, bigint), support.forget_purge() to authenticated;
