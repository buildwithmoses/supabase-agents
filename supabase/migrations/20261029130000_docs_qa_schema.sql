-- docs_qa: Courtline help center for the docs Q&A agent (pgvector + full-text hybrid search, RLS by audience).
-- Idempotent: safe to re-run (the ingest script applies it before loading).

create extension if not exists vector with schema extensions;
create schema if not exists docs_qa;

create table if not exists docs_qa.articles (
  id            text primary key,
  title         text not null,
  audience      text not null check (audience in ('public', 'staff')),
  status        text not null check (status in ('current', 'deprecated')),
  superseded_by text[] not null default '{}',
  updated_at    date not null default current_date
);

create table if not exists docs_qa.chunks (
  id          text primary key,                       -- "<article_id>#<n>"
  article_id  text not null references docs_qa.articles(id) on delete cascade,
  heading     text not null,
  content     text not null,
  audience    text not null check (audience in ('public', 'staff')),  -- copied from the article for the RLS policy
  flagged     boolean not null default false,          -- ingestion removed text addressed to an AI
  embedding   extensions.vector(384) not null,
  fts         tsvector generated always as (to_tsvector('english', heading || ' ' || content)) stored
);

create index if not exists chunks_embedding_hnsw on docs_qa.chunks using hnsw (embedding extensions.vector_cosine_ops);
create index if not exists chunks_fts_gin on docs_qa.chunks using gin (fts);

-- Who is club staff. Only readable through docs_qa.is_staff() (security definer); no direct grants.
create table if not exists docs_qa.staff_members (user_id uuid primary key);
alter table docs_qa.staff_members enable row level security;

create or replace function docs_qa.is_staff() returns boolean
language sql stable security definer set search_path = docs_qa, public
as $$ select exists (select 1 from docs_qa.staff_members where user_id = auth.uid()) $$;

alter table docs_qa.articles enable row level security;
alter table docs_qa.chunks   enable row level security;

drop policy if exists articles_read on docs_qa.articles;
create policy articles_read on docs_qa.articles for select to authenticated
  using (audience = 'public' or docs_qa.is_staff());

drop policy if exists chunks_read on docs_qa.chunks;
create policy chunks_read on docs_qa.chunks for select to authenticated
  using (audience = 'public' or docs_qa.is_staff());

-- Hybrid search: full-text (OR of the question's lexemes) + vector, merged with reciprocal rank fusion.
-- SECURITY INVOKER, so the caller's RLS applies: a player never gets staff-only chunks back.
-- Deprecated articles are ordered after every current one, so they can never outrank current docs.
create or replace function docs_qa.hybrid_search(
  query_text text,
  query_embedding extensions.vector(384),
  match_count int default 8,
  rrf_k int default 50
) returns table (id text, article_id text, title text, heading text, content text,
                 deprecated boolean, superseded_by text[], audience text, score double precision)
language sql stable security invoker set search_path = docs_qa, extensions, public
as $$
  with q as (
    select nullif(replace(plainto_tsquery('english', query_text)::text, '&', '|'), '')::tsquery as tsq
  ),
  full_text as (
    select c.id, row_number() over (order by ts_rank_cd(c.fts, q.tsq) desc) as rank_ix
    from docs_qa.chunks c, q
    where q.tsq is not null and c.fts @@ q.tsq
    order by rank_ix
    limit match_count * 4
  ),
  semantic as (
    select c.id, row_number() over (order by c.embedding <=> query_embedding) as rank_ix
    from docs_qa.chunks c
    order by rank_ix
    limit match_count * 4
  )
  select c.id, c.article_id, a.title, c.heading, c.content,
         a.status = 'deprecated' as deprecated, a.superseded_by, c.audience,
         coalesce(1.0 / (rrf_k + ft.rank_ix), 0.0) + coalesce(1.0 / (rrf_k + s.rank_ix), 0.0) as score
  from full_text ft
  full outer join semantic s on ft.id = s.id
  join docs_qa.chunks c on c.id = coalesce(ft.id, s.id)
  join docs_qa.articles a on a.id = c.article_id
  order by (a.status = 'deprecated') asc, score desc, c.id
  limit match_count
$$;

grant usage on schema docs_qa to authenticated;
grant select on docs_qa.articles, docs_qa.chunks to authenticated;
grant execute on function docs_qa.hybrid_search(text, extensions.vector, int, int) to authenticated;
grant execute on function docs_qa.is_staff() to authenticated;
