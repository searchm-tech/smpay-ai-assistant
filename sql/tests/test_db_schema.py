"""실제 Supabase DB에 대한 스키마·권한·함수 동작 테스트 (DATABASE_ADMIN_URL 등이 없으면 건너뜀)."""

import psycopg
import pytest

from tests.conftest import (
    DIM,
    MODEL,
    REVISION,
    TEST_PREFIX,
    chunk,
    count_chunks,
    count_docs,
    match,
    replace,
    unit,
    vec_literal,
)

pytestmark = pytest.mark.db

DENIED = psycopg.errors.InsufficientPrivilege


# ---------- 권한 ----------

@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_anon_and_authenticated_cannot_touch_tables_or_functions(db, role):
    admin = db.admin
    # 사전 확인: SET ROLE 자체가 되어야 이 테스트가 의미 있다 (안 되면 권한 오류를 통과로 잘못 볼 수 있음)
    with admin.transaction(force_rollback=True):
        admin.execute(f"set local role {role}")

    statements = [
        "select 1 from public.documents",
        "select 1 from public.chunks",
        "select 1 from public.schema_migrations",
        "insert into public.documents (notion_page_id, title, url, content_hash) values ('__test__x', 't', 'u', 'h')",
        "delete from public.chunks",
        f"select * from public.match_chunks('{vec_literal(unit(0))}'::extensions.vector, 'r')",
        "select public.delete_documents_not_in(array['x'])",
    ]
    for stmt in statements:
        with pytest.raises(DENIED):
            with admin.transaction(force_rollback=True):
                admin.execute(f"set local role {role}")
                admin.execute(stmt)


def test_chat_reader_can_only_search(db):
    chat = db.chat
    assert match(chat, unit(0)) == []  # 호출은 가능 (테스트 revision의 데이터가 아직 없음)

    for stmt in [
        "select 1 from public.chunks",
        "select 1 from public.documents",
        "insert into public.documents (notion_page_id, title, url, content_hash) values ('__test__x', 't', 'u', 'h')",
        "update public.chunks set content = 'x'",
        "delete from public.chunks",
        "select public.replace_document('__test__a', 't', 'u', null, null, 'h', 'sig', '[]'::jsonb)",
        "select public.delete_documents_not_in(array['x'])",
    ]:
        with pytest.raises(DENIED):
            chat.execute(stmt)


def test_ingest_writer_can_write_but_cannot_read_or_search(db):
    assert replace(db.ingest, "w1", [chunk(0, unit(0))])
    for stmt in ["select 1 from public.chunks", "select 1 from public.documents"]:
        with pytest.raises(DENIED):
            db.ingest.execute(stmt)
    with pytest.raises(DENIED):
        match(db.ingest, unit(0))


def test_roles_have_statement_timeouts(db):
    cfg = dict(
        db.admin.execute(
            "select rolname, rolconfig from pg_roles where rolname in ('chat_reader', 'ingest_writer')"
        ).fetchall()
    )
    assert "statement_timeout=5s" in cfg["chat_reader"]
    assert "statement_timeout=2min" in cfg["ingest_writer"] or "statement_timeout=120s" in cfg["ingest_writer"]


# ---------- 입력 검증과 원자성 ----------

def test_wrong_embedding_dimension_is_rejected_and_nothing_is_written(db):
    with pytest.raises(psycopg.Error):
        replace(db.ingest, "dim", [chunk(0, [0.1, 0.2, 0.3])])
    assert count_docs(db.admin, "dim") == 0


def test_duplicate_chunk_index_is_rejected_and_nothing_is_written(db):
    with pytest.raises(psycopg.errors.UniqueViolation):
        replace(db.ingest, "dup", [chunk(0, unit(0)), chunk(0, unit(1))])
    assert count_docs(db.admin, "dup") == 0


def test_empty_chunk_content_is_rejected(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        replace(db.ingest, "empty", [chunk(0, unit(0), content="")])


def test_chunks_must_be_a_json_array(db):
    with pytest.raises(psycopg.Error):
        db.ingest.execute(
            "select public.replace_document(%s, 't', 'u', null, null, 'h', 'sig', '{}'::jsonb)", (TEST_PREFIX + "obj",)
        )
    assert count_docs(db.admin, "obj") == 0


def test_failed_replace_keeps_previous_document_and_chunks(db):
    replace(db.ingest, "atom", [chunk(0, unit(0), content="원본0"), chunk(1, unit(1), content="원본1")], "v1")
    with pytest.raises(psycopg.Error):
        replace(db.ingest, "atom", [chunk(0, unit(0), content="새0"), chunk(1, [0.1, 0.2], content="새1")], "v2")

    rows = db.admin.execute(
        "select c.content, d.content_hash from public.chunks c join public.documents d on d.id = c.document_id "
        "where d.notion_page_id = %s order by c.chunk_index",
        (TEST_PREFIX + "atom",),
    ).fetchall()
    assert rows == [("원본0", "v1"), ("원본1", "v1")]


def test_resync_is_idempotent_and_replaces_chunks(db):
    payload = [chunk(0, unit(0)), chunk(1, unit(1))]
    id1 = replace(db.ingest, "resync", payload)
    id2 = replace(db.ingest, "resync", payload)
    assert id1 == id2 and count_chunks(db.admin, id1) == 2  # 재실행해도 문서 id가 같고 청크가 늘지 않음

    replace(db.ingest, "resync", [chunk(0, unit(0))])
    assert count_chunks(db.admin, id1) == 1  # 줄어든 만큼 반영(남은 옛 청크 없음)


def test_deleting_a_document_cascades_to_its_chunks(db):
    doc = replace(db.ingest, "casc", [chunk(0, unit(0)), chunk(1, unit(1))])
    db.admin.execute("delete from public.documents where id = %s", (doc,))
    assert count_chunks(db.admin, doc) == 0


# ---------- 검색 ----------

def test_match_orders_by_cosine_similarity_and_reports_similarity(db):
    a = unit(0)
    b = [0.0] * DIM
    b[0], b[1] = 0.8, 0.6  # query(unit 0)와의 cosine = 0.8
    c = unit(2)  # cosine = 0
    replace(db.ingest, "rank", [chunk(0, c, content="C"), chunk(1, b, content="B"), chunk(2, a, content="A")])

    rows = match(db.chat, unit(0))
    assert [r[2] for r in rows] == ["A", "B", "C"]
    assert [round(r[3], 3) for r in rows] == [1.0, 0.8, 0.0]


def test_match_only_compares_chunks_with_the_same_revision(db):
    replace(
        db.ingest,
        "rev",
        [chunk(0, unit(0), content="현재", revision=REVISION), chunk(1, unit(0), content="옛모델", revision="test-rev-old")],
    )
    assert [r[2] for r in match(db.chat, unit(0), revision=REVISION)] == ["현재"]
    assert [r[2] for r in match(db.chat, unit(0), revision="test-rev-old")] == ["옛모델"]


def test_match_limit_is_respected_and_clamped_to_at_least_one(db):
    replace(db.ingest, "lim", [chunk(i, unit(i), content=f"c{i}") for i in range(4)])
    assert len(match(db.chat, unit(0), limit=2)) == 2
    assert len(match(db.chat, unit(0), limit=0)) == 1
    assert len(match(db.chat, unit(0), limit=-5)) == 1


def test_match_candidate_filter_and_empty_list_means_no_candidates(db):
    d1 = replace(db.ingest, "f1", [chunk(0, unit(0), content="F1")])
    replace(db.ingest, "f2", [chunk(0, unit(0), content="F2")])

    assert [r[2] for r in match(db.chat, unit(0), ids=[d1])] == ["F1"]
    assert match(db.chat, unit(0), ids=[]) == []  # 빈 배열이 "제한 없음"으로 해석되면 권한 우회가 된다
    assert {r[2] for r in match(db.chat, unit(0))} == {"F1", "F2"}


def test_match_with_wrong_query_dimension_raises_even_when_nothing_matches(db):
    with pytest.raises(psycopg.Error) as e:
        match(db.chat, [0.1, 0.2, 0.3], revision="test-rev-none-exist")
    assert "1024" in str(e.value)


# ---------- 동기화 정리 ----------

def test_delete_documents_not_in_removes_only_unlisted_documents_and_refuses_empty_list(db):
    for name in ("k1", "k2", "k3"):
        replace(db.ingest, name, [chunk(0, unit(0), content=name)])

    with pytest.raises(psycopg.Error):
        db.ingest.execute("select public.delete_documents_not_in(%s)", ([],))
    assert count_docs(db.admin, "k2") == 1  # 빈 목록 거부 → 아무것도 지워지지 않음

    # 나중에 실제 문서가 들어 있어도 지워지지 않도록, 롤백되는 트랜잭션 안에서 관리자 권한으로 검증한다.
    with db.admin.transaction(force_rollback=True):
        keep = [
            r[0]
            for r in db.admin.execute(
                "select notion_page_id from public.documents where notion_page_id <> %s", (TEST_PREFIX + "k2",)
            )
        ]
        assert db.admin.execute("select public.delete_documents_not_in(%s)", (keep,)).fetchone()[0] == 1
        assert count_docs(db.admin, "k2") == 0 and count_docs(db.admin, "k1") == 1
    assert count_docs(db.admin, "k2") == 1  # 롤백되어 원복


def test_model_and_revision_are_stored_per_chunk(db):
    doc = replace(db.ingest, "meta", [chunk(0, unit(0))])
    row = db.admin.execute(
        "select embedding_model, embedding_revision from public.chunks where document_id = %s", (doc,)
    ).fetchone()
    assert row == (MODEL, REVISION)
