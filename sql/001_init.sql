-- 001_init.sql
-- documents / chunks 테이블, 검색·수집 함수, 최소 권한 역할.
-- 추가 전용 마이그레이션: 적용한 뒤에는 이 파일을 수정하지 말고 새 번호 파일을 추가한다.
-- 역할 비밀번호는 여기에 쓰지 않는다 (apply_migrations.py 가 .env 값으로 설정).

create extension if not exists vector with schema extensions;

-- ---------------------------------------------------------------- 테이블

create table public.documents (
  id uuid primary key default gen_random_uuid(),
  notion_page_id text not null unique,
  title text not null,
  url text not null,
  breadcrumb text,                         -- 상위 페이지 경로 (출처 표기용)
  notion_last_edited_at timestamptz,
  content_hash text not null,              -- 정제 텍스트 해시: Notion 수정시각 대신 "실제 내용 변경"을 감지
  synced_at timestamptz not null default now()
);

create table public.chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references public.documents (id) on delete cascade,
  chunk_index integer not null check (chunk_index >= 0),
  heading_path text,                       -- "문서 > H2 > H3"
  content text not null check (length(content) > 0),
  token_count integer not null check (token_count >= 0),
  truncated boolean not null default false,
  embedding extensions.vector(1024) not null,
  embedding_model text not null,           -- 임베딩 서비스 응답의 model / revision 을 그대로 저장
  embedding_revision text not null,        -- 모델을 바꿨을 때 재임베딩 대상을 찾고, 검색은 같은 revision끼리만 비교
  created_at timestamptz not null default now(),
  unique (document_id, chunk_index)
);

-- RLS를 켜고 정책은 만들지 않는다: anon / authenticated 는 접근 불가.
alter table public.documents enable row level security;
alter table public.chunks enable row level security;
revoke all on table public.documents, public.chunks from public, anon, authenticated;

-- ---------------------------------------------------------------- 역할 (비밀번호 없이 생성 → 설정 전에는 로그인 불가)

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'chat_reader') then
    create role chat_reader login noinherit nosuperuser nocreatedb nocreaterole;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'ingest_writer') then
    create role ingest_writer login noinherit nosuperuser nocreatedb nocreaterole;
  end if;
end
$$;

alter role chat_reader set statement_timeout = '5s';
alter role ingest_writer set statement_timeout = '120s';

grant usage on schema public, extensions to chat_reader, ingest_writer;

-- ---------------------------------------------------------------- 함수
-- 모두 security definer(테이블 소유자 권한으로 실행) + search_path 고정.
-- 역할은 테이블에 직접 접근하지 못하고 아래 함수만 실행할 수 있다.

-- 문서 upsert + 청크 교체를 한 트랜잭션으로. 중간에 실패하면 전부 취소되어 옛 청크가 그대로 남는다.
create or replace function public.replace_document(
  p_notion_page_id text,
  p_title text,
  p_url text,
  p_breadcrumb text,
  p_notion_last_edited_at timestamptz,
  p_content_hash text,
  p_chunks jsonb
) returns uuid
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_document_id uuid;
begin
  if p_chunks is null or jsonb_typeof(p_chunks) <> 'array' then
    raise exception 'p_chunks must be a JSON array';
  end if;

  insert into public.documents (
    notion_page_id, title, url, breadcrumb, notion_last_edited_at, content_hash, synced_at
  ) values (
    p_notion_page_id, p_title, p_url, p_breadcrumb, p_notion_last_edited_at, p_content_hash, now()
  )
  on conflict (notion_page_id) do update set
    title = excluded.title,
    url = excluded.url,
    breadcrumb = excluded.breadcrumb,
    notion_last_edited_at = excluded.notion_last_edited_at,
    content_hash = excluded.content_hash,
    synced_at = now()
  returning id into v_document_id;

  delete from public.chunks where document_id = v_document_id;

  insert into public.chunks (
    document_id, chunk_index, heading_path, content, token_count, truncated,
    embedding, embedding_model, embedding_revision
  )
  select
    v_document_id,
    (c ->> 'chunk_index')::integer,
    c ->> 'heading_path',
    c ->> 'content',
    (c ->> 'token_count')::integer,
    coalesce((c ->> 'truncated')::boolean, false),
    (c -> 'embedding')::text::extensions.vector(1024),
    c ->> 'embedding_model',
    c ->> 'embedding_revision'
  from jsonb_array_elements(p_chunks) as c;

  return v_document_id;
end;
$$;

-- cosine 유사도 검색 (similarity = 1 - cosine 거리). 같은 revision의 청크끼리만 비교한다.
-- 검색 벡터 차원이 틀리면 오류를 낸다(청크가 없을 때 조용히 빈 결과가 나오는 것을 막아 임베딩 버그를 바로 드러낸다).
-- p_document_ids 는 지금은 항상 null(전원 동일 권한)이지만, 나중에 권한 필터를 붙일 자리다. 빈 배열은 "후보 없음"이다.
create or replace function public.match_chunks(
  p_embedding extensions.vector(1024),
  p_revision text,
  p_limit integer default 8,
  p_document_ids uuid[] default null
) returns table (
  chunk_id uuid,
  document_id uuid,
  title text,
  url text,
  breadcrumb text,
  heading_path text,
  content text,
  similarity double precision
)
language plpgsql
stable
security definer
set search_path = public, extensions
as $$
begin
  if vector_dims(p_embedding) <> 1024 then
    raise exception 'query embedding must have 1024 dimensions';
  end if;

  return query
  select
    c.id,
    c.document_id,
    d.title,
    d.url,
    d.breadcrumb,
    c.heading_path,
    c.content,
    1 - (c.embedding <=> p_embedding)
  from public.chunks c
  join public.documents d on d.id = c.document_id
  where c.embedding_revision = p_revision
    and (p_document_ids is null or c.document_id = any (p_document_ids))
  order by c.embedding <=> p_embedding
  limit least(greatest(p_limit, 1), 50);
end;
$$;

-- 동기화 때 Notion에서 사라진 문서를 지운다(청크는 cascade). 삭제 건수를 반환.
-- 호출하는 쪽은 Notion 목록 조회가 "전부 성공했을 때만" 호출해야 한다(일부만 받은 목록으로 부르면 문서가 잘못 삭제됨).
create or replace function public.delete_documents_not_in(p_keep_notion_page_ids text[])
returns integer
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_deleted integer;
begin
  if p_keep_notion_page_ids is null or cardinality(p_keep_notion_page_ids) = 0 then
    raise exception 'refusing to delete all documents: keep-list is empty';
  end if;

  with deleted as (
    delete from public.documents
    where notion_page_id <> all (p_keep_notion_page_ids)
    returning 1
  )
  select count(*)::integer into v_deleted from deleted;

  return v_deleted;
end;
$$;

-- ---------------------------------------------------------------- 함수 실행 권한 (기본은 PUBLIC 실행 가능이므로 먼저 회수)

revoke execute on function public.replace_document(text, text, text, text, timestamptz, text, jsonb)
  from public, anon, authenticated;
revoke execute on function public.match_chunks(extensions.vector, text, integer, uuid[])
  from public, anon, authenticated;
revoke execute on function public.delete_documents_not_in(text[])
  from public, anon, authenticated;

grant execute on function public.match_chunks(extensions.vector, text, integer, uuid[]) to chat_reader;
grant execute on function public.replace_document(text, text, text, text, timestamptz, text, jsonb) to ingest_writer;
grant execute on function public.delete_documents_not_in(text[]) to ingest_writer;
