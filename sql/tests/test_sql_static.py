"""마이그레이션 SQL이 보안 속성을 유지하는지 DB 없이 정적으로 검사한다. (실제 동작은 test_db_*.py)"""

import re
from pathlib import Path

SQL = (Path(__file__).resolve().parents[1] / "001_init.sql").read_text(encoding="utf-8")
FUNCTIONS = re.findall(r"create or replace function public\.(\w+)\s*\(", SQL, re.I)


def test_no_password_literal_in_migration():
    assert not re.search(r"password\s*'", SQL, re.I)
    assert "password" not in re.sub(r"--.*", "", SQL, flags=re.I)  # 주석 제외, 본문에 password 자체가 없어야 함


def test_both_tables_have_rls_enabled():
    assert re.search(r"alter table public\.documents enable row level security", SQL, re.I)
    assert re.search(r"alter table public\.chunks enable row level security", SQL, re.I)


def test_tables_are_revoked_from_anon_authenticated_and_public():
    assert re.search(r"revoke all on table public\.documents, public\.chunks from .*anon.*authenticated", SQL, re.I | re.S)


def test_chunks_embedding_column_is_1024_dimensional_and_not_null():
    # 함수 시그니처에도 vector(1024)가 나오므로, 반드시 chunks 테이블의 컬럼 정의에 묶어서 검사한다.
    table = re.search(r"create table public\.chunks \((.*?)\n\);", SQL, re.I | re.S)
    assert table and re.search(r"embedding\s+extensions\.vector\(1024\)\s+not null", table.group(1), re.I)


def test_chunks_cascade_with_documents():
    assert re.search(r"references public\.documents\s*\(id\)\s*on delete cascade", SQL, re.I)


def test_expected_functions_exist():
    assert {"replace_document", "match_chunks", "delete_documents_not_in"} <= set(FUNCTIONS)


def test_every_function_pins_search_path():
    # security definer 함수가 search_path 하이재킹에 노출되지 않도록 고정
    assert len(re.findall(r"set search_path\s*=", SQL, re.I)) >= len(FUNCTIONS)


def test_every_function_revokes_execute_from_public_anon_authenticated():
    for fn in FUNCTIONS:
        pattern = rf"revoke execute on function public\.{fn}\([^)]*\)\s+from[^;]*public[^;]*anon[^;]*authenticated"
        assert re.search(pattern, SQL, re.I | re.S), fn


def test_roles_are_created_without_password_and_with_statement_timeouts():
    assert re.search(r"create role chat_reader login", SQL, re.I)
    assert re.search(r"create role ingest_writer login", SQL, re.I)
    assert re.search(r"alter role chat_reader set statement_timeout", SQL, re.I)


def test_chat_reader_can_only_execute_match_chunks():
    grants = re.findall(r"grant execute on function public\.(\w+)\([^)]*\)\s+to\s+(\w+)", SQL, re.I)
    assert ("match_chunks", "chat_reader") in grants
    assert [fn for fn, role in grants if role == "chat_reader"] == ["match_chunks"]


def test_delete_documents_not_in_refuses_empty_list():
    # 수집 실패로 빈 목록이 넘어왔을 때 전체 삭제되는 사고 방지
    body = re.search(r"function public\.delete_documents_not_in.*?\$\$;", SQL, re.I | re.S)
    assert body and re.search(r"raise exception", body.group(0), re.I)


def test_match_chunks_rejects_wrong_query_dimension_explicitly():
    body = re.search(r"function public\.match_chunks.*?\n\$\$;", SQL, re.I | re.S)
    assert body and re.search(r"vector_dims\(p_embedding\)\s*<>\s*1024", body.group(0))
    assert re.search(r"raise exception", body.group(0), re.I)
