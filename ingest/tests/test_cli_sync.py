import json

import pytest

from notion_ingest.cli import main
from notion_ingest.embed_client import EmbedUnavailable
from notion_ingest.notion_client import NotionAuthError
from notion_ingest.store import StoreError
from tests.conftest import FakeNotion, child_tag
from tests.fakes import FakeEmbedder, FakeStore
from tests.fixtures import SENTINEL, TOKEN, pid
from tests.test_sync import CARD, body, make_tree

SECRET = "e" * 40
DB_URL = "postgresql://ingest_writer:DbPassword-must-not-leak@db.example:5432/postgres"


def env(**kw):
    base = {
        "NOTION_TOKEN": TOKEN, "NOTION_ROOT_PAGE_IDS": pid(1), "EMBEDDER_SECRET": SECRET, "INGEST_DATABASE_URL": DB_URL,
    }
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


class Harness:
    def __init__(self, notion=None, embedder=None, store=None):
        self.notion = notion or make_tree(FakeNotion())
        self.embedder = embedder or FakeEmbedder()
        self.store = store or FakeStore()
        self.out: list[str] = []
        self.err: list[str] = []
        self.store_error: Exception | None = None

    def run(self, argv, environ=None):
        def store_factory(settings):
            if self.store_error:
                raise self.store_error
            return self.store

        code = main(
            ["sync", *argv], environ=environ if environ is not None else env(),
            client_factory=lambda settings: self.notion, embedder_factory=lambda settings: self.embedder,
            store_factory=store_factory, out=self.out.append, err=self.err.append,
        )
        return code, "\n".join(self.out), "\n".join(self.err)


def assert_no_leaks(*texts):
    blob = "\n".join(texts)
    for secret in (TOKEN, SECRET, "DbPassword-must-not-leak", SENTINEL):
        assert secret not in blob


def test_a_successful_sync_prints_counts_and_exits_zero_without_leaking_anything():
    h = Harness()
    code, out, err = h.run([])
    assert code == 0 and "완전 성공" in out and "신규 3" in out and len(h.store.docs) == 3
    assert h.store.closed
    assert_no_leaks(out, err)


def test_dry_run_prints_a_plan_and_writes_nothing():
    h = Harness()
    code, out, _ = h.run(["--dry-run"])
    assert code == 0 and "dry-run" in out and "예상 청크" in out and "신규 3" in out
    assert h.store.docs == {} and set(h.store.kinds()) <= {"state"} and h.embedder.calls == []


def test_a_partial_run_exits_one_and_lists_failed_documents_by_id_and_kind():
    h = Harness(embedder=FakeEmbedder())
    h.embedder.errors = [EmbedUnavailable("network")]
    code, out, _ = h.run([])
    assert code == 1 and "일부 완료" in out and "network" in out and "doc_failures" in out
    assert_no_leaks(out)


def test_quarantined_documents_are_listed_by_id_and_pattern_name_only():
    h = Harness(notion=make_tree(FakeNotion(), extra={2: "\n\n" + CARD}))
    code, out, _ = h.run([])
    assert code == 0 and pid(2) in out and "card" in out and "4111" not in out
    assert pid(2) not in h.store.docs


def test_the_allow_list_from_the_environment_lets_a_sensitive_document_through():
    h = Harness(notion=make_tree(FakeNotion(), extra={2: "\n\n" + CARD}))
    code, out, _ = h.run([], env(INGEST_ALLOW_SENSITIVE_PAGE_IDS=pid(2)))
    assert code == 0 and pid(2) in h.store.docs and "격리 0" in out


def test_json_output_is_valid_and_content_free():
    h = Harness()
    code, out, _ = h.run(["--json"])
    data = json.loads(out)
    assert code == 0 and data["counts"]["new"] == 3 and data["status"] == "complete" and SENTINEL not in out


def test_limit_is_applied_and_validated():
    h = Harness(notion=make_tree(FakeNotion(), 4))
    code, out, _ = h.run(["--limit", "2"])
    assert code == 0 and len(h.store.docs) == 2 and "미처리 3" in out
    for bad in ("0", "-1", "abc"):
        code, _, _ = Harness().run(["--limit", bad])
        assert code == 2


def test_force_reindex_and_force_delete_flags_take_effect():
    h = Harness()
    h.run([])
    h.store.reset_calls()
    code, out, _ = h.run(["--force-reindex"])
    assert code == 0 and "변경 3" in out
    big = Harness(notion=make_tree(FakeNotion(), 50))
    big.run([])
    big.notion = make_tree(FakeNotion(), 39)
    code, out, _ = big.run([])
    assert code == 1 and "delete_guard" in out and "--force-delete" in out
    code, out, _ = big.run(["--force-delete"])
    assert code == 0 and "삭제 11" in out


@pytest.mark.parametrize("override", [{"EMBEDDER_SECRET": None}, {"EMBEDDER_SECRET": "short"}, {"INGEST_DATABASE_URL": None}, {"NOTION_TOKEN": None}])
def test_configuration_errors_exit_two_and_leak_nothing(override):
    code, out, err = Harness().run([], env(**override))
    assert code == 2 and err and "short" not in err
    assert_no_leaks(out, err)


def test_a_database_connection_failure_exits_one_with_only_the_error_kind():
    h = Harness()
    h.store_error = StoreError("OperationalError", "08006")
    code, out, err = h.run([])
    assert code == 1 and "OperationalError" in err and h.embedder.calls == []
    assert_no_leaks(out, err)


def test_an_unreachable_embedder_is_reported_as_the_reason():
    h = Harness()
    h.embedder.probe_error = EmbedUnavailable("network")
    code, out, _ = h.run([])
    assert code == 1 and "embedder_unavailable" in out and "실패" in out and h.store.docs == {}


def test_a_notion_auth_failure_points_at_the_token_and_permission():
    h = Harness()
    h.notion.page_errors[pid(1)] = NotionAuthError("unauthorized", 401)
    code, out, _ = h.run([])
    assert code == 1 and "notion_auth" in out and "Read content" in out
    assert_no_leaks(out)


def test_a_locked_run_says_another_sync_is_running():
    h = Harness()
    h.store.locked_by_other = True
    code, out, _ = h.run([])
    assert code == 1 and "locked" in out and "다른 동기화" in out


def test_the_max_pages_setting_limits_the_crawl_and_blocks_deletion():
    h = Harness()
    code, out, _ = h.run([], env(NOTION_MAX_PAGES="1"))
    assert code == 1 and "crawl_max_pages" in out and len(h.store.docs) == 1
