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
