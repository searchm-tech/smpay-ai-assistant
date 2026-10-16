-- 002_sync.sql
-- 동기화(A6)용: 인덱스 서명, 동기화 상태 조회, 메타데이터 갱신, 문서 삭제, 실행 기록.
-- 추가 전용 마이그레이션: 적용한 뒤에는 이 파일을 수정하지 말고 새 번호 파일을 추가한다.

-- ---------------------------------------------------------------- 인덱스 서명
-- 서명 = 임베딩 모델 revision + 청크 규칙 버전 + 임베딩 입력 방식. 서명이 바뀌면 내용 해시가 같아도 재색인한다.
-- null 은 "서명을 모름"(옛 데이터)이며 재색인 대상이다.
alter table public.documents add column index_signature text;

-- ---------------------------------------------------------------- replace_document: 서명 인자 추가
-- 인자 목록이 바뀌므로 옛 7인자 버전을 지운다(남겨 두면 서명 없이 쓰는 경로가 살아 있게 된다).
drop function public.replace_document(text, text, text, text, timestamptz, text, jsonb);

create or replace function public.replace_document(
  p_notion_page_id text,
  p_title text,
  p_url text,
  p_breadcrumb text,
  p_notion_last_edited_at timestamptz,
  p_content_hash text,
  p_index_signature text,
  p_chunks jsonb
) returns uuid
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_document_id uuid;
begin
  if p_index_signature is null or p_index_signature = '' then
    raise exception 'p_index_signature must not be empty';
  end if;
  if p_chunks is null or jsonb_typeof(p_chunks) <> 'array' then
    raise exception 'p_chunks must be a JSON array';
  end if;

  insert into public.documents (
    notion_page_id, title, url, breadcrumb, notion_last_edited_at, content_hash, index_signature, synced_at
  ) values (
    p_notion_page_id, p_title, p_url, p_breadcrumb, p_notion_last_edited_at, p_content_hash, p_index_signature, now()
  )
  on conflict (notion_page_id) do update set
    title = excluded.title,
    url = excluded.url,
    breadcrumb = excluded.breadcrumb,
    notion_last_edited_at = excluded.notion_last_edited_at,
    content_hash = excluded.content_hash,
    index_signature = excluded.index_signature,
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

-- ---------------------------------------------------------------- 동기화 상태 조회
-- 변경 판단에 필요한 최소 정보만 돌려준다(본문·벡터 없음). ingest_writer 는 테이블을 직접 읽지 못하므로 이 함수가 유일한 경로다.
create or replace function public.get_sync_state()
returns table (
  notion_page_id text,
  notion_last_edited_at timestamptz,
  content_hash text,
  title text,
  breadcrumb text,
  index_signature text
)
language sql
stable
security definer
set search_path = public, extensions
as $$
  select d.notion_page_id, d.notion_last_edited_at, d.content_hash, d.title, d.breadcrumb, d.index_signature
  from public.documents d;
$$;

-- ---------------------------------------------------------------- 메타데이터만 갱신 (재임베딩 없음)
-- 제목·URL·경로·수정시각만 바뀌고 본문은 같을 때 쓴다. 청크·해시·서명은 건드리지 않는다. 문서가 없으면 false.
create or replace function public.update_document_metadata(
  p_notion_page_id text,
  p_title text,
  p_url text,
  p_breadcrumb text,
  p_notion_last_edited_at timestamptz
) returns boolean
language plpgsql
security definer
set search_path = public, extensions
as $$
begin
  update public.documents
  set title = p_title,
      url = p_url,
      breadcrumb = p_breadcrumb,
      notion_last_edited_at = p_notion_last_edited_at,
      synced_at = now()
  where notion_page_id = p_notion_page_id;
  return found;
end;
$$;

-- ---------------------------------------------------------------- 문서 하나 삭제 (청크는 cascade)
-- 민감정보가 새로 발견된 문서를 크롤 완전성과 상관없이 즉시 검색에서 빼기 위한 경로. 없으면 false.
create or replace function public.delete_document(p_notion_page_id text)
returns boolean
language plpgsql
security definer
set search_path = public, extensions
as $$
begin
  delete from public.documents where notion_page_id = p_notion_page_id;
  return found;
end;
$$;

-- ---------------------------------------------------------------- 실행 기록
-- 개수와 상태, 종류 코드만 담는다. 문서 id·제목·본문을 담을 컬럼이 없다.
create table public.sync_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running' check (status in ('running', 'complete', 'partial', 'failed')),
  new_count integer not null default 0 check (new_count >= 0),
  changed_count integer not null default 0 check (changed_count >= 0),
  metadata_only_count integer not null default 0 check (metadata_only_count >= 0),
  unchanged_count integer not null default 0 check (unchanged_count >= 0),
  deleted_count integer not null default 0 check (deleted_count >= 0),
  quarantined_count integer not null default 0 check (quarantined_count >= 0),
  failed_count integer not null default 0 check (failed_count >= 0),
  reasons text[] not null default '{}'
);

alter table public.sync_runs enable row level security;
revoke all on table public.sync_runs from public, anon, authenticated;

create or replace function public.start_sync_run()
returns uuid
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_id uuid;
begin
  insert into public.sync_runs default values returning id into v_id;
  return v_id;
end;
$$;

-- 끝난 실행은 다시 끝낼 수 없다. reasons 는 [a-z0-9_]{1,40} 형태의 종류 코드만 허용(자유 텍스트 차단).
create or replace function public.finish_sync_run(
  p_run_id uuid,
  p_status text,
  p_new integer,
  p_changed integer,
  p_metadata_only integer,
  p_unchanged integer,
  p_deleted integer,
  p_quarantined integer,
  p_failed integer,
  p_reasons text[]
) returns void
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_reason text;
begin
  if p_status not in ('complete', 'partial', 'failed') then
    raise exception 'invalid final status';
  end if;
  if p_reasons is not null then
    if cardinality(p_reasons) > 20 then
      raise exception 'too many reasons';
    end if;
    foreach v_reason in array p_reasons loop
      if v_reason is null or v_reason !~ '^[a-z0-9_]{1,40}$' then
        raise exception 'reasons must be short lowercase codes';
      end if;
    end loop;
  end if;

  update public.sync_runs
  set status = p_status,
      finished_at = now(),
      new_count = p_new,
      changed_count = p_changed,
      metadata_only_count = p_metadata_only,
      unchanged_count = p_unchanged,
      deleted_count = p_deleted,
      quarantined_count = p_quarantined,
      failed_count = p_failed,
      reasons = coalesce(p_reasons, '{}')
  where id = p_run_id and status = 'running';

  if not found then
    raise exception 'run not found or already finished';
  end if;
end;
$$;

-- ---------------------------------------------------------------- 함수 실행 권한 (먼저 회수 → ingest_writer 에게만 부여)

revoke execute on function public.replace_document(text, text, text, text, timestamptz, text, text, jsonb)
  from public, anon, authenticated;
revoke execute on function public.get_sync_state() from public, anon, authenticated;
revoke execute on function public.update_document_metadata(text, text, text, text, timestamptz)
  from public, anon, authenticated;
revoke execute on function public.delete_document(text) from public, anon, authenticated;
revoke execute on function public.start_sync_run() from public, anon, authenticated;
revoke execute on function public.finish_sync_run(uuid, text, integer, integer, integer, integer, integer, integer, integer, text[])
  from public, anon, authenticated;

grant execute on function public.replace_document(text, text, text, text, timestamptz, text, text, jsonb) to ingest_writer;
grant execute on function public.get_sync_state() to ingest_writer;
grant execute on function public.update_document_metadata(text, text, text, text, timestamptz) to ingest_writer;
grant execute on function public.delete_document(text) to ingest_writer;
grant execute on function public.start_sync_run() to ingest_writer;
grant execute on function public.finish_sync_run(uuid, text, integer, integer, integer, integer, integer, integer, integer, text[]) to ingest_writer;
