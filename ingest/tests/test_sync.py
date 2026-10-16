import logging

import pytest

from notion_ingest.chunker import chunk_document
from notion_ingest.embed_client import EmbedAuthError, EmbedRevisionChanged, EmbedUnavailable
from notion_ingest.notion_client import NotionAuthError, NotionError
from notion_ingest.store import StoreError
from notion_ingest.sync import SyncOptions, make_signature, run_sync
from tests.conftest import FakeNotion, child_tag
from tests.fakes import FakeEmbedder, FakeStore
from tests.fixtures import SENTINEL, pid
from tests.test_read_only import RecordingNotion

CARD = "결제 테스트 카드번호는 4111 1111 1111 1111 입니다."


def body(label: str, sentences: int = 6) -> str:
    return " ".join(f"{label}의 {i}번째 안내 문장은 충전 한도와 승인 절차를 설명합니다." for i in range(sentences))


def make_tree(fake: FakeNotion, n_children: int = 2, extra: dict[int, str] | None = None) -> FakeNotion:
    ids = range(2, 2 + n_children)
    tags = "\n".join(child_tag(i, f"문서{i}") for i in ids)
    fake.add(1, "루트", f"# 루트\n\n{body('루트')}\n\n{tags}")
    for i in ids:
        fake.add(i, f"문서{i}", f"# 문서{i}\n\n{body(f'문서{i}')}" + (extra or {}).get(i, ""), parent=1)
    return fake


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def embedder():
    return FakeEmbedder()


@pytest.fixture
def notion():
    return make_tree(FakeNotion())


def sync(notion, embedder, store, **opts):
    return run_sync(notion, embedder, store, [pid(1)], SyncOptions(**opts))


# ---------- 첫 동기화 ----------

def test_first_sync_indexes_every_document_with_chunks_signature_and_real_token_counts(notion, embedder, store):
    s = sync(notion, embedder, store)
    assert (s.status, s.exit_code, s.reasons) == ("complete", 0, [])
    assert s.counts == {"new": 3, "changed": 0, "metadata_only": 0, "unchanged": 0, "deleted": 0, "quarantined": 0, "failed": 0}
    assert set(store.docs) == {pid(1), pid(2), pid(3)}
    for page_id, doc in store.docs.items():
        assert doc.index_signature == make_signature("rev-1")
        records = store.chunks[page_id]
        assert [r["chunk_index"] for r in records] == list(range(len(records))) and records
        for r in records:
            assert r["embedding_model"] == "fake-kure" and r["embedding_revision"] == "rev-1"
            assert r["token_count"] == len(f"{r['heading_path']}\n{r['content']}")  # 추정이 아니라 서버가 센 값
            assert r["truncated"] is False and len(r["embedding"]) == 4
    assert store.docs[pid(2)].breadcrumb == "루트" and store.docs[pid(1)].breadcrumb is None


def test_a_run_is_recorded_from_start_to_finish_with_the_counts(notion, embedder, store):
    s = sync(notion, embedder, store)
    assert store.kinds().index("lock") < store.kinds().index("start_run") < store.kinds().index("finish_run")
    assert store.runs[s.run_id]["status"] == "complete" and store.runs[s.run_id]["counts"]["new"] == 3
    assert embedder.probes == 1


def test_the_embedding_input_is_title_path_plus_content_and_content_alone_is_stored(notion, embedder, store):
    sync(notion, embedder, store)
    expected = chunk_document("문서2", "# 문서2\n\n" + body("문서2"))
    texts = embedder.embedded_texts()
    assert all(c.embed_text in texts for c in expected)
    stored = store.chunks[pid(2)]
    assert [r["content"] for r in stored] == [c.content for c in expected]
    assert all(not r["content"].startswith("문서2\n") for r in stored)


# ---------- 변경 감지 ----------

def test_a_second_sync_with_no_changes_embeds_and_writes_nothing(notion, embedder, store):
    sync(notion, embedder, store)
    embedder.calls.clear()
    store.reset_calls()
    s = sync(notion, embedder, store)
    assert s.counts["unchanged"] == 3 and s.counts["new"] == 0 and s.status == "complete"
    assert embedder.calls == []
    assert not {"replace", "update_meta", "delete", "delete_not_in"} & set(store.kinds())


def test_only_the_changed_document_is_reembedded_and_replaced(notion, embedder, store):
    sync(notion, embedder, store)
    embedder.calls.clear()
    store.reset_calls()
    notion.pages[pid(2)]["markdown"] += "\n\n" + body("추가된 내용")
    notion.pages[pid(2)]["last_edited"] = "2026-10-05T00:00:00.000Z"
    s = sync(notion, embedder, store)
    assert s.counts["changed"] == 1 and s.counts["unchanged"] == 2
    assert store.replaced() == [pid(2)]
    assert all("문서3" not in t and "루트" not in t.split("\n")[0] for t in embedder.embedded_texts())


def test_a_metadata_only_change_updates_without_embedding(notion, embedder, store):
    sync(notion, embedder, store)
    embedder.calls.clear()
    store.reset_calls()
    notion.pages[pid(3)]["last_edited"] = "2026-10-09T00:00:00.000Z"
    s = sync(notion, embedder, store)
    assert s.counts["metadata_only"] == 1 and embedder.calls == []
    assert ("update_meta", pid(3)) in store.calls and "replace" not in store.kinds()
    assert store.stored_time(pid(3)) == "2026-10-09T00:00:00.000Z"


def test_a_title_change_reembeds_because_the_title_is_part_of_the_input(notion, embedder, store):
    sync(notion, embedder, store)
    embedder.calls.clear()
    notion.pages[pid(3)]["title"] = "새 제목"
    s = sync(notion, embedder, store)
    assert s.counts["changed"] == 1 and any(t.startswith("새 제목") for t in embedder.embedded_texts())
    assert all(r["heading_path"].startswith("새 제목") for r in store.chunks[pid(3)])


def test_a_new_embedding_revision_reindexes_everything_even_when_text_is_identical(notion, store):
    sync(notion, FakeEmbedder("rev-1"), store)
    store.reset_calls()
    s = sync(notion, FakeEmbedder("rev-2"), store)
    assert s.counts["changed"] == 3 and sorted(store.replaced()) == sorted([pid(1), pid(2), pid(3)])
    assert {d.index_signature for d in store.docs.values()} == {make_signature("rev-2")}
    assert make_signature("rev-1") != make_signature("rev-2")


def test_force_reindex_and_legacy_documents_without_a_signature_are_reindexed(notion, embedder, store):
    sync(notion, embedder, store)
    store.reset_calls()
    assert sync(notion, embedder, store, force_reindex=True).counts["changed"] == 3
    store.docs[pid(2)] = type(store.docs[pid(2)])(
        store.docs[pid(2)].last_edited_time, store.docs[pid(2)].content_hash, store.docs[pid(2)].title,
        store.docs[pid(2)].breadcrumb, None,
    )
    store.reset_calls()
    s = sync(notion, embedder, store)
    assert s.counts["changed"] == 1 and store.replaced() == [pid(2)]


# ---------- 삭제 ----------

def test_documents_removed_from_notion_are_deleted_only_after_a_complete_crawl(notion, embedder, store):
    sync(notion, embedder, store)
    store.reset_calls()
    smaller = make_tree(FakeNotion(), n_children=1)
    s = sync(smaller, embedder, store)
    assert s.counts["deleted"] == 1 and set(store.docs) == {pid(1), pid(2)}
    keep = next(c[1] for c in store.calls if c[0] == "delete_not_in")
    assert set(keep) == {pid(1), pid(2)}


def test_nothing_is_deleted_when_the_crawl_was_incomplete(notion, embedder, store):
    sync(notion, embedder, store)
    store.reset_calls()
    smaller = make_tree(FakeNotion(), n_children=1)
    smaller.add(5, "깨진 문서", "# x", parent=1)
    smaller.pages[pid(1)]["markdown"] += "\n" + child_tag(5, "깨진 문서")
    smaller.page_errors[pid(5)] = NotionError("http_500", 500)
    s = sync(smaller, embedder, store)
    assert "delete_not_in" not in store.kinds() and pid(3) in store.docs
    assert "crawl_errors" in s.reasons and s.status == "partial" and s.exit_code == 1


def test_nothing_is_deleted_when_any_document_failed_to_write(notion, embedder, store):
    sync(notion, embedder, store)
    store.reset_calls()
    smaller = make_tree(FakeNotion(), n_children=1)
    smaller.pages[pid(2)]["markdown"] += "\n\n" + body("수정")
    store.fail_replace[pid(2)] = StoreError("OperationalError", "08006")
    s = sync(smaller, embedder, store)
    assert "delete_not_in" not in store.kinds() and pid(3) in store.docs
    assert s.counts["failed"] == 1 and s.failed == [(pid(2), "OperationalError")]
    assert "doc_failures" in s.reasons and s.exit_code == 1


@pytest.mark.parametrize(
    "before, after, allowed",
    [(50, 39, False), (50, 41, True), (2, 0, False), (3, 2, True), (120, 105, False)],  # 마지막: 20%(24) 미만이지만 10개 초과
)
def test_mass_deletion_guard(embedder, store, before, after, allowed):
    sync(make_tree(FakeNotion(), before), embedder, store)
    store.reset_calls()
    s = sync(make_tree(FakeNotion(), after), embedder, store)
    if allowed:
        assert s.counts["deleted"] == before - after and "delete_guard" not in s.reasons
    else:
        assert s.counts["deleted"] == 0 and "delete_guard" in s.reasons and s.exit_code == 1
        assert "delete_not_in" not in store.kinds() and len(store.docs) == before + 1
        forced = sync(make_tree(FakeNotion(), after), embedder, store, force_delete=True)
        assert forced.counts["deleted"] == before - after and len(store.docs) == after + 1


# ---------- 민감정보 격리 ----------

def test_a_document_with_a_high_risk_pattern_is_quarantined_not_indexed(embedder, store):
    notion = make_tree(FakeNotion(), extra={2: "\n\n" + CARD})
    s = sync(notion, embedder, store)
    assert s.counts["quarantined"] == 1 and s.counts["new"] == 2
    assert s.quarantined == [(pid(2), ("card",))]
    assert pid(2) not in store.docs and all(CARD not in t for t in embedder.embedded_texts())
    assert s.status == "complete" and s.exit_code == 0


def test_medium_risk_patterns_such_as_email_and_phone_do_not_block_indexing(embedder, store):
    notion = make_tree(FakeNotion(), extra={2: "\n\n문의: help@example.com / 02-1234-5678"})
    s = sync(notion, embedder, store)
    assert s.counts["quarantined"] == 0 and pid(2) in store.docs


def test_a_previously_indexed_document_that_turns_sensitive_is_removed_immediately(notion, embedder, store):
    sync(notion, embedder, store)
    store.reset_calls()
    notion.pages[pid(2)]["markdown"] += "\n\n" + CARD
    s = sync(notion, embedder, store)
    assert ("delete", pid(2)) in store.calls and pid(2) not in store.docs
    assert s.counts["quarantined"] == 1 and "replace" not in store.kinds()


def test_quarantine_applies_to_unchanged_documents_when_the_allow_entry_is_removed(embedder, store):
    notion = make_tree(FakeNotion(), extra={2: "\n\n" + CARD})
    sync(notion, embedder, store, allow_sensitive=frozenset({pid(2)}))
    assert pid(2) in store.docs
    store.reset_calls()
    s = sync(notion, embedder, store)  # 허용 목록 없이 다시: 내용은 그대로지만 검색에서 빠져야 한다
    assert pid(2) not in store.docs and s.counts["quarantined"] == 1


def test_an_allow_listed_sensitive_document_is_indexed(embedder, store):
    notion = make_tree(FakeNotion(), extra={2: "\n\n" + CARD})
    s = sync(notion, embedder, store, allow_sensitive=frozenset({pid(2)}))
    assert s.counts["quarantined"] == 0 and pid(2) in store.docs


def test_a_quarantined_document_is_removed_individually_and_is_not_in_the_keep_list(embedder, store):
    sync(make_tree(FakeNotion(), 3, extra={2: "\n\n" + CARD}), embedder, store, allow_sensitive=frozenset({pid(2)}))
    assert set(store.docs) == {pid(1), pid(2), pid(3), pid(4)}
    store.reset_calls()
    s = sync(make_tree(FakeNotion(), 2, extra={2: "\n\n" + CARD}), embedder, store)  # 허용 해제 + 문서 4 삭제
    keep = next(c[1] for c in store.calls if c[0] == "delete_not_in")
    assert pid(2) not in keep and set(keep) == {pid(1), pid(3)}
    assert set(store.docs) == {pid(1), pid(3)}
    assert (s.counts["quarantined"], s.counts["deleted"]) == (1, 1)


# ---------- 잘림(truncated) 재분할 ----------

def test_a_truncated_chunk_is_split_in_half_and_reembedded(notion, store):
    embedder = FakeEmbedder(truncate_over=150)  # 문서 하나의 임베딩 입력(약 227자)이 한 번은 잘리고, 반으로 나누면 들어간다
    sync(notion, embedder, store)
    whole = chunk_document("문서2", "# 문서2\n\n" + body("문서2"))
    records = store.chunks[pid(2)]
    assert len(whole) == 1 and len(records) == 2
    assert all(not r["truncated"] for r in records)
    assert [r["chunk_index"] for r in records] == [0, 1]
    assert " ".join(" ".join(r["content"].split()) for r in records) == " ".join(whole[0].content.split())  # 나눠도 글자는 그대로


def test_splitting_is_bounded_and_the_truncated_flag_is_kept_when_it_still_does_not_fit(notion, store):
    embedder = FakeEmbedder(truncate_over=10)
    s = sync(notion, embedder, store)
    assert s.status == "complete"
    records = store.chunks[pid(2)]
    assert all(r["truncated"] for r in records)
    n_chunks = len(chunk_document("문서2", "# 문서2\n\n" + body("문서2")))
    assert len(records) <= n_chunks * 4  # 최대 2번 나눈다


# ---------- 임베딩 서버 장애 ----------

def test_one_document_failing_to_embed_does_not_stop_the_others(notion, embedder, store):
    embedder.errors = [EmbedUnavailable("network")]
    s = sync(notion, embedder, store)
    assert s.counts["failed"] == 1 and s.counts["new"] == 2 and len(store.docs) == 2
    assert s.status == "partial" and "doc_failures" in s.reasons and s.exit_code == 1
    assert s.failed[0][1] == "network"


def test_three_consecutive_embedder_failures_abort_the_run(embedder, store):
    notion = make_tree(FakeNotion(), n_children=6)
    embedder.errors = [EmbedUnavailable("busy")] * 10
    s = sync(notion, embedder, store)
    assert s.status == "failed" and "embedder_unavailable" in s.reasons and s.exit_code == 1
    assert len(embedder.calls) == 3 and store.docs == {}
    assert store.runs[s.run_id]["status"] == "failed"


def test_an_embedder_auth_failure_aborts_immediately(notion, embedder, store):
    embedder.errors = [EmbedAuthError("unauthorized", 401)]
    s = sync(notion, embedder, store)
    assert s.status == "failed" and "embedder_auth" in s.reasons and len(embedder.calls) == 1


def test_a_revision_change_mid_run_aborts_and_writes_nothing_for_that_document(notion, embedder, store):
    embedder.errors = [None, EmbedRevisionChanged("revision_changed", 200)]
    s = sync(notion, embedder, store)
    assert s.status == "failed" and "revision_changed" in s.reasons
    assert len(store.docs) == 1


def test_an_unreachable_embedder_stops_before_touching_the_database_or_notion(notion, embedder, store):
    embedder.probe_error = EmbedUnavailable("network")
    s = sync(notion, embedder, store)
    assert s.status == "failed" and s.exit_code == 1 and "embedder_unavailable" in s.reasons
    assert store.calls == [] and notion.calls == [] and s.run_id is None


def test_a_bad_embedder_secret_is_reported_at_preflight(notion, embedder, store):
    embedder.probe_error = EmbedAuthError("unauthorized", 401)
    s = sync(notion, embedder, store)
    assert "embedder_auth" in s.reasons and store.calls == []


# ---------- 잠금 · 제한 · 드라이런 ----------

def test_when_another_sync_holds_the_lock_nothing_runs(notion, embedder, store):
    store.locked_by_other = True
    s = sync(notion, embedder, store)
    assert s.status == "failed" and "locked" in s.reasons and s.exit_code == 1
    assert store.kinds() == ["lock"] and notion.calls == [] and embedder.calls == [] and s.run_id is None


def test_limit_processes_only_n_documents_and_never_deletes(embedder, store):
    notion = make_tree(FakeNotion(), n_children=4)
    s = sync(notion, embedder, store, limit=2)
    assert s.counts["new"] == 2 and s.deferred == 3 and len(store.docs) == 2
    assert s.reasons == ["limited"] and s.status == "partial" and s.exit_code == 0
    assert "delete_not_in" not in store.kinds()
    s2 = sync(notion, embedder, store, limit=2)  # 이어서 처리된다
    assert s2.counts["new"] == 2 and s2.counts["unchanged"] == 2 and len(store.docs) == 4


def test_dry_run_plans_without_embedding_locking_or_writing(notion, embedder, store):
    s = sync(notion, embedder, store, dry_run=True)
    assert s.dry_run and s.counts["new"] == 3 and s.estimated_chunks >= 3
    assert embedder.calls == [] and embedder.probes == 1
    assert set(store.kinds()) <= {"state"} and store.docs == {} and s.run_id is None
    assert s.status == "complete" and s.exit_code == 0


def test_dry_run_reports_what_would_be_deleted_and_quarantined(embedder, store):
    sync(make_tree(FakeNotion(), 3), embedder, store)
    store.reset_calls()
    notion = make_tree(FakeNotion(), 2, extra={2: "\n\n" + CARD})
    s = sync(notion, embedder, store, dry_run=True)
    assert s.counts["deleted"] == 0 and s.delete_candidates == 1 and s.counts["quarantined"] == 1
    assert set(store.kinds()) <= {"state"}


# ---------- Notion·저장소 오류와 기록 ----------

def test_a_notion_auth_failure_ends_the_run_as_failed_and_deletes_nothing(notion, embedder, store):
    sync(notion, embedder, store)
    store.reset_calls()
    notion.page_errors[pid(1)] = NotionAuthError("unauthorized", 401)
    s = sync(notion, embedder, store)
    assert s.status == "failed" and "notion_auth" in s.reasons and s.exit_code == 1
    assert store.runs[s.run_id]["status"] == "failed" and "delete_not_in" not in store.kinds()


def test_a_store_failure_after_the_run_started_still_finishes_the_run_record(notion, embedder, store):
    store.fail_state = StoreError("OperationalError", "08006")
    s = sync(notion, embedder, store)
    assert s.status == "failed" and "store_error" in s.reasons
    assert store.runs[s.run_id]["status"] == "failed"


def test_an_interrupt_finishes_the_run_record_and_propagates(notion, embedder, store):
    embedder.errors = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        sync(notion, embedder, store)
    (run,) = store.runs.values()
    assert run["status"] == "failed" and run["reasons"] == ["interrupted"]


def test_a_failure_to_record_the_run_does_not_crash_the_sync(notion, embedder, store):
    store.fail_finish = StoreError("OperationalError", "08006")
    s = sync(notion, embedder, store)
    assert s.status == "complete" and len(store.docs) == 3


def test_reasons_are_always_valid_short_codes(embedder, store):
    import re

    notion = make_tree(FakeNotion(), n_children=6)
    embedder.errors = [EmbedUnavailable("busy")] * 10
    s = sync(notion, embedder, store)
    assert all(re.fullmatch(r"[a-z0-9_]{1,40}", r) for r in s.reasons)


# ---------- 비밀·본문 노출 방지, Notion 읽기 전용 ----------

def test_logs_and_summary_never_contain_titles_or_document_text(embedder, store, caplog):
    notion = FakeNotion()
    notion.add(1, "루트", f"# 루트\n\n{body('루트')}\n\n{child_tag(2, SENTINEL)}")
    notion.add(2, SENTINEL, f"# {SENTINEL}\n\n{SENTINEL} {body('x')}", parent=1)
    embedder.errors = [None, EmbedUnavailable("busy")]
    caplog.set_level(logging.DEBUG)
    s = sync(notion, embedder, store)
    assert s.failed and SENTINEL not in caplog.text
    assert SENTINEL not in repr(s) and SENTINEL not in str(s.failed)


def test_a_full_sync_sends_only_get_requests_to_notion(embedder, store):
    server = RecordingNotion()
    s = run_sync(server.client(), embedder, store, [pid(1)], SyncOptions())
    assert s.counts["new"] == 2
    assert server.requests and {m for m, _ in server.requests} == {"GET"}


# ---------- 변형 테스트로 보강한 케이스 ----------

def test_the_sync_level_deletion_guard_does_not_rely_on_the_crawler_alone(notion, embedder, store, monkeypatch):
    import notion_ingest.sync as sync_module

    sync(notion, embedder, store)
    store.reset_calls()
    real = sync_module.crawl

    def lying_crawl(*args, **kwargs):  # 크롤러가 불완전한데도 삭제 후보를 내보내는 버그를 흉내 낸다
        result = real(*args, **kwargs)
        result.incomplete_reasons = ["errors"]
        result.deletable_ids = [pid(3)]
        return result

    monkeypatch.setattr(sync_module, "crawl", lying_crawl)
    s = sync(notion, embedder, store)
    assert "delete_not_in" not in store.kinds() and pid(3) in store.docs and s.counts["deleted"] == 0


def test_a_limited_run_does_not_delete_even_if_a_document_disappeared(embedder, store):
    sync(make_tree(FakeNotion(), 4), embedder, store)
    store.reset_calls()
    smaller = make_tree(FakeNotion(), 3)  # 문서 5 삭제
    for i in (2, 3):
        smaller.pages[pid(i)]["markdown"] += "\n\n" + body(f"수정{i}")
    s = sync(smaller, embedder, store, limit=1)
    assert s.deferred == 2 and "delete_not_in" not in store.kinds() and pid(5) in store.docs  # 루트도 하위 목록이 바뀌어 changed


def test_an_aborted_run_does_not_delete_even_if_a_document_disappeared(embedder, store):
    sync(make_tree(FakeNotion(), 4), embedder, store)
    store.reset_calls()
    smaller = make_tree(FakeNotion(), 3)
    smaller.pages[pid(2)]["markdown"] += "\n\n" + body("수정")
    embedder.errors = [EmbedAuthError("unauthorized", 401)]
    s = sync(smaller, embedder, store)
    assert s.status == "failed" and s.counts["failed"] == 0
    assert "delete_not_in" not in store.kinds() and pid(5) in store.docs


def test_a_breadcrumb_only_change_updates_the_stored_breadcrumb_without_embedding(embedder, store):
    sync(make_tree(FakeNotion(), 2), embedder, store)
    embedder.calls.clear()
    moved = FakeNotion()  # 문서 3이 루트 아래에서 문서 2 아래로 이동
    moved.add(1, "루트", f"# 루트\n\n{body('루트')}\n\n{child_tag(2, '문서2')}")
    moved.add(2, "문서2", f"# 문서2\n\n{body('문서2')}\n\n{child_tag(3, '문서3')}", parent=1)
    moved.add(3, "문서3", f"# 문서3\n\n{body('문서3')}", parent=2)
    s = sync(moved, embedder, store)
    assert s.counts["metadata_only"] == 1 and s.counts["changed"] == 2  # 루트(자식 목록 줄)와 문서2가 바뀜
    assert store.docs[pid(3)].breadcrumb == "루트 > 문서2"
    assert all("문서3의" not in t for t in embedder.embedded_texts())  # 문서 3 본문은 다시 임베딩하지 않았다


def test_an_embedding_response_with_a_different_revision_than_the_preflight_aborts(notion, embedder, store):
    embedder.embed_revision = "rev-other"
    s = sync(notion, embedder, store)
    assert s.status == "failed" and "revision_changed" in s.reasons and store.docs == {}
