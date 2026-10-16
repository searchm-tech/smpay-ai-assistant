import pytest

from notion_ingest.notion_client import (
    MarkdownResult,
    NotionError,
    NotionNotFound,
    PageMeta,
)
from tests.fixtures import page_url, pid


class FakeNotion:
    """Notion 클라이언트 대역. 호출 기록을 남겨 '호출이 없었음'을 검증할 수 있다."""

    def __init__(self):
        self.pages: dict[str, dict] = {}
        self.subtrees: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.page_errors: dict[str, Exception] = {}
        self.markdown_errors: dict[str, Exception] = {}

    def add(self, n, title, markdown="", *, parent=None, parent_type="page_id", last_edited="2026-10-01T00:00:00.000Z",
            truncated=False, unknown_ids=(), in_trash=False):
        pages_id = pid(n)
        self.pages[pages_id] = dict(
            title=title, markdown=markdown, parent_id=pid(parent) if parent is not None else None,
            parent_type=parent_type if parent is not None else "workspace", last_edited=last_edited,
            truncated=truncated, unknown_ids=tuple(unknown_ids), in_trash=in_trash, url=page_url(n, title),
        )
        return pages_id

    # --- 클라이언트 인터페이스 ---
    def get_page(self, page_id: str) -> PageMeta:
        self.calls.append(("page", page_id))
        if page_id in self.page_errors:
            raise self.page_errors[page_id]
        p = self.pages.get(page_id)
        if p is None:
            raise NotionNotFound("not_found", 404)
        return PageMeta(
            id=page_id, url=p["url"], title=p["title"], last_edited_time=p["last_edited"],
            parent_type=p["parent_type"], parent_id=p["parent_id"], in_trash=p["in_trash"],
        )

    def get_markdown(self, page_id: str) -> MarkdownResult:
        self.calls.append(("markdown", page_id))
        if page_id in self.markdown_errors:
            raise self.markdown_errors[page_id]
        if page_id in self.subtrees:
            return MarkdownResult(markdown=self.subtrees[page_id], truncated=False, unknown_block_ids=())
        p = self.pages.get(page_id)
        if p is None:
            raise NotionNotFound("not_found", 404)
        return MarkdownResult(markdown=p["markdown"], truncated=p["truncated"], unknown_block_ids=p["unknown_ids"])

    # --- 검증 도우미 ---
    def count(self, kind: str, page_id: str | None = None) -> int:
        return sum(1 for k, pid_ in self.calls if k == kind and (page_id is None or pid_ == page_id))

    def touched(self, page_id: str) -> bool:
        return any(pid_ == page_id for _, pid_ in self.calls)


@pytest.fixture
def fake():
    return FakeNotion()


def child_tag(n: int, title: str) -> str:
    return f'<page url="{page_url(n, title)}">{title}</page>'


# ---------- 실제 Postgres 에 대한 테스트 공통 설정 ----------
# DATABASE_ADMIN_URL + INGEST_WRITER_PASSWORD (또는 INGEST_DATABASE_URL) 가 없으면 건너뛴다.
# 마이그레이션 001·002 가 적용되어 있어야 한다(`cd sql && .venv/bin/python apply_migrations.py`).

import os  # noqa: E402

import psycopg  # noqa: E402

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(scope="session")
def db_urls():
    from notion_ingest.config import ConfigError, writer_dsn

    admin = os.environ.get("DATABASE_ADMIN_URL")
    if not admin:
        pytest.skip("DATABASE_ADMIN_URL 이 없어 DB 테스트를 건너뜀")
    try:
        writer = writer_dsn(os.environ)
    except ConfigError:
        pytest.skip("ingest_writer 접속 설정이 없어 DB 테스트를 건너뜀")
    with psycopg.connect(admin, autocommit=True, connect_timeout=15) as conn:
        try:
            ready = conn.execute(
                "select count(*) from public.schema_migrations where version in ('001', '002')"
            ).fetchone()[0] == 2
        except psycopg.errors.UndefinedTable:
            ready = False
    if not ready:
        pytest.skip("마이그레이션 001·002 가 적용되지 않음")
    return {"admin": admin, "writer": writer}


@pytest.fixture
def admin_conn(db_urls):
    with psycopg.connect(db_urls["admin"], autocommit=True, connect_timeout=15) as conn:
        yield conn


@pytest.fixture
def local_empty_db(db_urls, admin_conn):
    """e2e 는 '없는 문서를 지우는' 동기화를 실제로 실행하므로, 로컬 호스트의 빈 DB 에서만 돌린다(운영 데이터 보호)."""
    from psycopg.conninfo import conninfo_to_dict

    host = conninfo_to_dict(db_urls["admin"]).get("host", "")
    if host not in LOCAL_HOSTS:
        pytest.skip("로컬 DB 에서만 실행(운영 데이터 보호)")
    if admin_conn.execute("select count(*) from public.documents").fetchone()[0] != 0:
        pytest.skip("documents 가 비어 있지 않아 실행하지 않음(운영 데이터 보호)")
    yield admin_conn
    admin_conn.execute("delete from public.documents")
    admin_conn.execute("delete from public.sync_runs")
