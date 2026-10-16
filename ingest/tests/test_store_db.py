"""PgStore 가 실제 Postgres(ingest_writer 역할)에서 동작하는지 확인한다. 테스트 데이터는 notion_page_id 가 `__test__` 로 시작한다."""

import pytest
from psycopg.conninfo import conninfo_to_dict

from notion_ingest.store import PgStore, StoreError
from notion_ingest.timeutil import normalize_time
from tests.fixtures import SENTINEL

pytestmark = pytest.mark.db

PREFIX = "__test__"


def chunk(i=0, **kw):
    base = {
        "chunk_index": i, "heading_path": "제목", "content": "내용", "token_count": 3, "truncated": False,
        "embedding": [1.0] + [0.0] * 1023, "embedding_model": "test-model", "embedding_revision": "test-rev-store",
    }
    base.update(kw)
    return base


@pytest.fixture
def store(db_urls, admin_conn):
    admin_conn.execute("delete from public.documents where starts_with(notion_page_id, %s)", (PREFIX,))
    s = PgStore(db_urls["writer"])
    yield s
    s.close()
    admin_conn.execute("delete from public.documents where starts_with(notion_page_id, %s)", (PREFIX,))


def write(store, page_id="a", edited="2026-10-01T00:00:00.000Z", **kw):
    args = dict(
        page_id=PREFIX + page_id, title="제목", url="https://example.com", breadcrumb="루트 > 부모", last_edited_time=edited,
        content_hash="h1", index_signature="sig-1", chunks=[chunk()],
    )
    args.update(kw)
    store.replace_document(**args)


def test_replace_then_get_sync_state_round_trips_all_fields(store):
    write(store)
    s = store.get_sync_state()[PREFIX + "a"]
    assert (s.title, s.breadcrumb, s.content_hash, s.index_signature) == ("제목", "루트 > 부모", "h1", "sig-1")
    assert normalize_time(s.last_edited_time) == "2026-10-01T00:00:00.000Z"


@pytest.mark.parametrize("edited", [
    "2026-10-01T00:00:00.000Z", "2026-10-01T09:30:15.123+09:00", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00.5Z",
])
def test_the_last_edited_time_survives_the_database_with_the_same_instant(store, edited):
    write(store, edited=edited)
    stored = store.get_sync_state()[PREFIX + "a"].last_edited_time
    assert normalize_time(stored) == normalize_time(edited)  # 크롤러가 같은 순간을 같다고 볼 수 있어야 매번 metadata_only 로 오분류되지 않는다


def test_a_missing_last_edited_time_is_stored_as_null(store):
    write(store, edited=None)
    assert store.get_sync_state()[PREFIX + "a"].last_edited_time is None


def test_update_metadata_and_delete(store):
    write(store)
    assert store.update_metadata(page_id=PREFIX + "a", title="새 제목", url="u2", breadcrumb=None, last_edited_time="2026-10-02T00:00:00Z")
    s = store.get_sync_state()[PREFIX + "a"]
    assert (s.title, s.breadcrumb, s.content_hash, s.index_signature) == ("새 제목", None, "h1", "sig-1")
    assert store.update_metadata(page_id=PREFIX + "ghost", title="t", url="u", breadcrumb=None, last_edited_time=None) is False
    assert store.delete_document(PREFIX + "a") is True and store.delete_document(PREFIX + "a") is False
    assert PREFIX + "a" not in store.get_sync_state()


def test_run_records_round_trip_and_only_hold_counts(store, admin_conn):
    run_id = store.start_run()
    try:
        store.finish_run(run_id, "partial", {"new": 3, "changed": 2, "failed": 1}, ["doc_failures", "limited"])
        row = admin_conn.execute(
            "select status, new_count, changed_count, metadata_only_count, failed_count, reasons from public.sync_runs where id = %s",
            (run_id,),
        ).fetchone()
        assert row == ("partial", 3, 2, 0, 1, ["doc_failures", "limited"])
        with pytest.raises(StoreError):
            store.finish_run(run_id, "complete", {}, [])  # 끝난 실행은 다시 닫을 수 없다
    finally:
        admin_conn.execute("delete from public.sync_runs where id = %s", (run_id,))


def test_database_errors_carry_only_the_kind_and_sqlstate(store):
    with pytest.raises(StoreError) as e:
        write(store, chunks=[chunk(content=SENTINEL, embedding=[1.0] * 8)])  # 차원 오류
    assert e.value.sqlstate and SENTINEL not in str(e.value) + repr(e.value)
    assert PREFIX + "a" not in store.get_sync_state()  # 원자적: 문서 행도 남지 않는다


def test_a_constraint_violation_does_not_leak_the_failing_row_into_the_error(store):
    # Postgres 는 CHECK 위반 시 "Failing row contains (...본문...)" 을 메시지에 싣는다. StoreError 에는 담기면 안 된다.
    with pytest.raises(StoreError) as e:
        write(store, title=SENTINEL, chunks=[chunk(content=SENTINEL, token_count=-1)])
    assert e.value.sqlstate == "23514" and SENTINEL not in str(e.value) + repr(e.value)


def test_connection_failures_and_repr_never_expose_the_password(db_urls):
    info = conninfo_to_dict(db_urls["writer"])
    with pytest.raises(StoreError) as e:
        PgStore(db_urls["writer"].replace(info["password"], "wrong-password-xyz"))
    text = str(e.value) + repr(e.value)
    assert info["password"] not in text and "wrong-password-xyz" not in text
    assert info["host"] not in text and info["user"] not in text  # 연결 오류 원문(호스트·사용자명)을 그대로 옮기지 않는다
    ok = PgStore(db_urls["writer"])
    try:
        assert info["password"] not in repr(ok)
    finally:
        ok.close()


def test_the_advisory_lock_is_exclusive_and_released_on_close(db_urls, local_empty_db):
    first, second = PgStore(db_urls["writer"]), PgStore(db_urls["writer"])
    try:
        assert first.try_lock() is True
        assert second.try_lock() is False
        first.close()
        third = PgStore(db_urls["writer"])
        try:
            assert third.try_lock() is True
        finally:
            third.close()
    finally:
        first.close()
        second.close()
