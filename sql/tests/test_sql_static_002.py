"""002_sync.sql 이 보안 속성을 유지하는지 DB 없이 정적으로 검사한다. (실제 동작은 test_db_sync.py)"""

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1]
SQL = (SQL_DIR / "002_sync.sql").read_text(encoding="utf-8")
CODE = re.sub(r"--.*", "", SQL)  # 주석 제외
FUNCTIONS = re.findall(r"create or replace function public\.(\w+)\s*\(", SQL, re.I)
ALL_SQL = "\n".join(p.read_text(encoding="utf-8") for p in sorted(SQL_DIR.glob("[0-9][0-9][0-9]_*.sql")))


def test_expected_functions_exist():
    assert {
        "replace_document", "get_sync_state", "update_document_metadata", "delete_document",
        "start_sync_run", "finish_sync_run",
    } == set(FUNCTIONS)


def test_no_password_in_migration():
    assert "password" not in CODE.lower()


def test_documents_get_an_index_signature_column_and_the_old_replace_signature_is_dropped():
    assert re.search(r"alter table public\.documents\s+add column index_signature text", CODE, re.I)
    assert re.search(
        r"drop function public\.replace_document\(text, text, text, text, timestamptz, text, jsonb\)", CODE, re.I
    )
    assert re.search(r"p_index_signature text,\s*p_chunks jsonb", CODE, re.I)


def test_replace_document_rejects_an_empty_signature():
    body = re.search(r"function public\.replace_document.*?\n\$\$;", CODE, re.I | re.S).group(0)
    assert re.search(r"p_index_signature is null", body, re.I) and "raise exception" in body.lower()


def test_sync_runs_has_rls_and_is_revoked():
    assert re.search(r"alter table public\.sync_runs enable row level security", CODE, re.I)
    assert re.search(r"revoke all on table public\.sync_runs from[^;]*public[^;]*anon[^;]*authenticated", CODE, re.I | re.S)


def test_sync_runs_holds_only_counts_status_and_reason_codes():
    table = re.search(r"create table public\.sync_runs \((.*?)\n\);", CODE, re.I | re.S).group(1)
    columns = re.findall(r"^\s{2}(\w+)\s+(\w+(?:\[\])?)", table, re.M)
    allowed_text = {"status": "text", "reasons": "text[]"}
    for name, kind in columns:
        if name in allowed_text:
            assert kind == allowed_text[name], name
        else:
            assert kind in {"uuid", "timestamptz", "integer"}, (name, kind)  # 본문·제목·id를 담을 자리가 없다
    assert re.search(r"check \(status in \('running', 'complete', 'partial', 'failed'\)\)", table, re.I)


def test_finish_sync_run_validates_reason_codes():
    body = re.search(r"function public\.finish_sync_run.*?\n\$\$;", CODE, re.I | re.S).group(0)
    assert "^[a-z0-9_]{1,40}$" in body and "raise exception" in body.lower()


def test_update_document_metadata_never_touches_chunks_or_content_hash():
    body = re.search(r"function public\.update_document_metadata.*?\n\$\$;", CODE, re.I | re.S).group(0)
    assert "chunks" not in body.lower() and "content_hash" not in body.lower() and "index_signature" not in body.lower()


def test_every_function_pins_search_path_and_is_security_definer():
    assert len(re.findall(r"set search_path\s*=\s*public, extensions", CODE, re.I)) == len(FUNCTIONS)
    assert len(re.findall(r"security definer", CODE, re.I)) == len(FUNCTIONS)


def test_every_function_revokes_execute_from_public_anon_authenticated_and_grants_only_ingest_writer():
    for fn in FUNCTIONS:
        assert re.search(
            rf"revoke execute on function public\.{fn}\([^)]*\)\s+from[^;]*public[^;]*anon[^;]*authenticated", CODE, re.I | re.S
        ), fn
        assert re.search(rf"grant execute on function public\.{fn}\([^)]*\)\s+to ingest_writer;", CODE, re.I), fn


def test_chat_reader_still_only_executes_match_chunks_across_all_migrations():
    grants = re.findall(r"grant execute on function public\.(\w+)\([^)]*\)\s+to\s+(\w+)", re.sub(r"--.*", "", ALL_SQL), re.I)
    assert [fn for fn, role in grants if role == "chat_reader"] == ["match_chunks"]
