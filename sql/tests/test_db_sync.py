"""002_sync.sql 의 함수·테이블 동작과 권한 (DATABASE_ADMIN_URL 등이 없으면 건너뜀)."""

import datetime as dt

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.conftest import TEST_PREFIX, chunk, count_chunks, replace, unit

pytestmark = pytest.mark.db

DENIED = psycopg.errors.InsufficientPrivilege
EDITED = dt.datetime(2026, 10, 1, 12, 30, 45, 123000, tzinfo=dt.timezone.utc)


def state(conn, page_id: str) -> dict | None:
    rows = conn.execute(
        "select notion_page_id, notion_last_edited_at, content_hash, title, breadcrumb, index_signature "
        "from public.get_sync_state() where notion_page_id = %s",
        (TEST_PREFIX + page_id,),
    ).fetchall()
    if not rows:
        return None
    keys = ("notion_page_id", "notion_last_edited_at", "content_hash", "title", "breadcrumb", "index_signature")
    return dict(zip(keys, rows[0]))


# ---------- replace_document 8인자 / 서명 ----------

def test_replace_stores_the_index_signature_and_get_sync_state_returns_it(db):
    replace(db.ingest, "sig", [chunk(0, unit(0))], "h1", signature="sigA")
    s = state(db.ingest, "sig")
    assert s["index_signature"] == "sigA" and s["content_hash"] == "h1" and s["title"] == "테스트 문서"
    replace(db.ingest, "sig", [chunk(0, unit(0))], "h1", signature="sigB")
    assert state(db.ingest, "sig")["index_signature"] == "sigB"


@pytest.mark.parametrize("signature", [None, ""])
def test_replace_rejects_a_missing_signature_and_writes_nothing(db, signature):
    with pytest.raises(psycopg.Error):
        replace(db.ingest, "nosig", [chunk(0, unit(0))], signature=signature)
    assert state(db.ingest, "nosig") is None


def test_the_old_seven_argument_replace_document_no_longer_exists(db):
    old = db.admin.execute(
        "select to_regprocedure('public.replace_document(text,text,text,text,timestamptz,text,jsonb)')"
    ).fetchone()[0]
    assert old is None


def test_get_sync_state_returns_the_last_edited_time_as_an_exact_instant(db):
    db.ingest.execute(
        "select public.replace_document(%s, 't', 'u', 'a > b', %s, 'h', 'sig', %s)",
        (TEST_PREFIX + "ts", EDITED, Jsonb([chunk(0, unit(0))])),
    )
    assert state(db.ingest, "ts")["notion_last_edited_at"] == EDITED
    assert state(db.ingest, "ts")["breadcrumb"] == "a > b"


# ---------- update_document_metadata ----------

def test_metadata_update_changes_only_metadata_and_keeps_chunks_hash_and_signature(db):
    doc_id = replace(db.ingest, "meta", [chunk(0, unit(0)), chunk(1, unit(1))], "h1", signature="sigA")
    found = db.ingest.execute(
        "select public.update_document_metadata(%s, '새 제목', 'https://new', '새 경로', %s)", (TEST_PREFIX + "meta", EDITED)
    ).fetchone()[0]
    assert found is True
    s = state(db.ingest, "meta")
    assert (s["title"], s["breadcrumb"], s["notion_last_edited_at"]) == ("새 제목", "새 경로", EDITED)
    assert (s["content_hash"], s["index_signature"]) == ("h1", "sigA")
    assert count_chunks(db.admin, doc_id) == 2


def test_metadata_update_of_an_unknown_page_returns_false_and_creates_nothing(db):
    assert db.ingest.execute(
        "select public.update_document_metadata(%s, 't', 'u', null, null)", (TEST_PREFIX + "ghost",)
    ).fetchone()[0] is False
    assert state(db.ingest, "ghost") is None


# ---------- delete_document ----------

def test_delete_document_removes_the_document_and_its_chunks_only(db):
    gone = replace(db.ingest, "gone", [chunk(0, unit(0))])
    kept = replace(db.ingest, "kept", [chunk(0, unit(1))])
    assert db.ingest.execute("select public.delete_document(%s)", (TEST_PREFIX + "gone",)).fetchone()[0] is True
    assert state(db.ingest, "gone") is None and count_chunks(db.admin, gone) == 0
    assert state(db.ingest, "kept") is not None and count_chunks(db.admin, kept) == 1
    assert db.ingest.execute("select public.delete_document(%s)", (TEST_PREFIX + "gone",)).fetchone()[0] is False


# ---------- sync_runs ----------

COUNTS = (3, 2, 1, 40, 0, 1, 0)  # new, changed, metadata_only, unchanged, deleted, quarantined, failed


def finish(conn, run_id, status="complete", counts=COUNTS, reasons=()):
    return conn.execute(
        "select public.finish_sync_run(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (run_id, status, *counts, list(reasons)),
    )


def row(admin, run_id):
    return admin.execute(
        "select status, finished_at is not null, new_count, changed_count, metadata_only_count, unchanged_count, "
        "deleted_count, quarantined_count, failed_count, reasons from public.sync_runs where id = %s", (run_id,)
    ).fetchone()


@pytest.fixture
def runs(db):
    """sync_runs 는 테스트 접두어가 없으므로, 테스트가 만든 run id 만 정확히 추적해서 지운다."""
    created: list = []

    def start(conn=None):
        run_id = (conn or db.ingest).execute("select public.start_sync_run()").fetchone()[0]
        created.append(run_id)
        return run_id

    db.start = start
    try:
        yield db
    finally:
        db.admin.execute("delete from public.sync_runs where id = any(%s)", (created,))


def test_a_run_starts_as_running_and_finishes_with_counts_and_reason_codes(runs):
    run_id = runs.start()
    assert row(runs.admin, run_id)[:2] == ("running", False)
    finish(runs.ingest, run_id, "partial", reasons=["errors", "max_pages"])
    assert row(runs.admin, run_id) == ("partial", True, *COUNTS, ["errors", "max_pages"])


def test_a_finished_run_cannot_be_finished_again(runs):
    run_id = runs.start()
    finish(runs.ingest, run_id)
    with pytest.raises(psycopg.Error):
        finish(runs.ingest, run_id, "failed")
    assert row(runs.admin, run_id)[0] == "complete"


def test_finishing_an_unknown_run_or_with_a_bad_status_is_rejected(runs):
    run_id = runs.start()
    with pytest.raises(psycopg.Error):
        finish(runs.ingest, "00000000-0000-0000-0000-000000000000")
    for status in ("running", "bogus"):
        with pytest.raises(psycopg.Error):
            finish(runs.ingest, run_id, status)
    assert row(runs.admin, run_id)[0] == "running"


@pytest.mark.parametrize("reasons", [["문서 본문 같은 설명"], ["has space"], ["x" * 41], ["UPPER"], [f"r{i}" for i in range(21)]])
def test_reason_codes_cannot_carry_free_text(runs, reasons):
    run_id = runs.start()
    with pytest.raises(psycopg.Error):
        finish(runs.ingest, run_id, reasons=reasons)
    assert row(runs.admin, run_id)[0] == "running"


@pytest.mark.parametrize("position", range(7))
def test_negative_counts_are_rejected_in_every_column(runs, position):
    run_id = runs.start()
    counts = [0] * 7
    counts[position] = -1
    with pytest.raises(psycopg.Error):
        finish(runs.ingest, run_id, counts=tuple(counts))
    assert row(runs.admin, run_id)[0] == "running"


# ---------- 권한 ----------

NEW_FUNCTION_CALLS = [
    "select * from public.get_sync_state()",
    "select public.update_document_metadata('x', 't', 'u', null, null)",
    "select public.delete_document('x')",
    "select public.start_sync_run()",
    "select public.finish_sync_run('00000000-0000-0000-0000-000000000000', 'complete', 0,0,0,0,0,0,0, '{}')",
]


@pytest.mark.parametrize("stmt", NEW_FUNCTION_CALLS)
def test_chat_reader_cannot_call_any_sync_function(db, stmt):
    with pytest.raises(DENIED):
        db.chat.execute(stmt)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_anon_and_authenticated_cannot_call_sync_functions_or_read_runs(db, role):
    for stmt in [*NEW_FUNCTION_CALLS, "select 1 from public.sync_runs"]:
        with pytest.raises(DENIED):
            with db.admin.transaction(force_rollback=True):
                db.admin.execute(f"set local role {role}")
                db.admin.execute(stmt)


def test_ingest_writer_cannot_read_sync_runs_directly(db):
    with pytest.raises(DENIED):
        db.ingest.execute("select 1 from public.sync_runs")
    with pytest.raises(DENIED):
        db.ingest.execute("insert into public.sync_runs (status) values ('complete')")
