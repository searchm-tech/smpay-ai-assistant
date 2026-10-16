"""허용 목록 루트에서 아래로 내려가며 페이지를 수집한다 (DB 접근 없음, 상태는 인자로 받는다)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from notion_ingest.cleaner import ChildRef, clean_markdown, content_hash
from notion_ingest.ids import normalize_page_id
from notion_ingest.notion_client import (
    MarkdownResult,
    NotionAuthError,
    NotionError,
    NotionNotFound,
    PageMeta,
)
from notion_ingest.scanner import scan_sensitive


class NotionLike(Protocol):
    def get_page(self, page_id: str) -> PageMeta: ...
    def get_markdown(self, page_id: str) -> MarkdownResult: ...


@dataclass(frozen=True)
class PrevDoc:
    """DB에 저장된 직전 상태 (A6의 get_sync_state()가 채운다)."""

    last_edited_time: str | None
    content_hash: str
    title: str
    breadcrumb: str | None


@dataclass
class NotionDocument:
    page_id: str
    title: str
    url: str
    breadcrumb: list[str]
    depth: int
    last_edited_time: str | None
    status: str  # new | changed | metadata_only | unchanged
    text: str | None  # new/changed 일 때만 채운다(재임베딩 대상)
    content_hash: str
    char_count: int
    unknown_count: int
    unknown_by_type: dict[str, int]
    truncated: bool
    partial: bool
    table_count: int
    sensitive_hits: dict[str, int]


@dataclass
class CrawlResult:
    documents: list[NotionDocument] = field(default_factory=list)
    databases_skipped: list[ChildRef] = field(default_factory=list)
    foreign_links_skipped: int = 0
    unverified_children_skipped: int = 0
    inaccessible_children: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (page_id, 오류 종류) — 본문·메시지는 담지 않는다
    incomplete_reasons: list[str] = field(default_factory=list)
    deletable_ids: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.incomplete_reasons


def crawl(
    client: NotionLike,
    root_ids: list[str] | tuple[str, ...],
    previous: dict[str, PrevDoc] | None = None,
    *,
    max_pages: int = 5000,
    max_depth: int = 30,
    max_followups: int = 20,
) -> CrawlResult:
    """BFS 크롤. 모든 페이지의 markdown을 매번 조회한다(자식은 부모 markdown의 <page> 태그로만 발견되므로
    변경 없는 페이지도 건너뛸 수 없다). 변경 판단은 정제 텍스트의 해시로 한다."""
    previous = previous or {}
    result = CrawlResult()
    reasons: set[str] = set()

    roots: list[str] = []
    for root in root_ids:
        normalized = normalize_page_id(root) or root
        if normalized not in roots:
            roots.append(normalized)

    # (페이지 id, 상위 제목 경로, 깊이, 기대하는 부모 id — 루트는 None)
    queue: deque[tuple[str, list[str], int, str | None]] = deque((r, [], 0, None) for r in roots)
    queued_pairs: set[tuple[str, str | None]] = {(r, None) for r in roots}
    seen: set[str] = set()
    databases: dict[str, ChildRef] = {}

    while queue:
        if len(result.documents) >= max_pages:
            reasons.add("max_pages")
            break
        page_id, crumb, depth, expected_parent = queue.popleft()
        is_root = expected_parent is None
        if page_id in seen:
            continue

        try:
            meta = client.get_page(page_id)
        except NotionAuthError:
            raise
        except NotionNotFound:
            if is_root:
                # 루트가 사라지면 하위 문서가 전부 "사라진 것"처럼 보인다 → 삭제를 막기 위해 불완전으로 표시
                result.errors.append((page_id, "root_not_found"))
                reasons.add("root_not_found")
            else:
                result.inaccessible_children += 1  # 접근 불가/삭제는 정상적인 제거로 본다
            continue
        except NotionError as e:
            result.errors.append((page_id, e.kind))
            reasons.add("errors")
            continue

        if meta.in_trash:
            continue

        if not is_root:
            # 허용 목록은 "태그가 보이는가"가 아니라 "실제 부모가 이 페이지인가"로 지킨다
            # (link_to_page 블록이 허용 목록 밖 페이지를 가리켜도 수집하지 않는다).
            if meta.parent_type == "page_id" and meta.parent_id == expected_parent:
                pass
            elif meta.parent_type == "page_id":
                result.foreign_links_skipped += 1
                continue
            else:  # block_id 등 — 부모를 싸게 검증할 수 없으므로 보수적으로 건너뛰고 센다
                result.unverified_children_skipped += 1
                continue

        try:
            fetched = client.get_markdown(page_id)
        except NotionAuthError:
            raise
        except NotionNotFound:
            result.errors.append((page_id, "markdown_not_found"))
            reasons.add("errors")
            continue
        except NotionError as e:
            result.errors.append((page_id, e.kind))
            reasons.add("errors")
            continue

        raw = fetched.markdown
        partial = False
        if fetched.truncated:  # 잘린 경우에만 unknown 블록을 후속 조회한다(미지원 블록까지 조회하면 호출이 폭증)
            followups = list(fetched.unknown_block_ids)
            if len(followups) > max_followups:
                partial = True
                followups = followups[:max_followups]
            for block_id in followups:
                try:
                    raw += "\n\n" + client.get_markdown(block_id).markdown
                except NotionAuthError:
                    raise
                except NotionNotFound:
                    continue  # 접근 불가 블록은 정상
                except NotionError as e:
                    result.errors.append((page_id, e.kind))
                    reasons.add("errors")
                    partial = True

        cleaned = clean_markdown(raw)
        digest = content_hash(cleaned.text)
        breadcrumb_text = " > ".join(crumb)
        prev = previous.get(page_id)
        if prev is None:
            status = "new"
        elif prev.content_hash != digest:
            status = "changed"
        elif (
            prev.last_edited_time == meta.last_edited_time
            and prev.title == meta.title
            and (prev.breadcrumb or "") == breadcrumb_text
        ):
            status = "unchanged"
        else:
            status = "metadata_only"

        result.documents.append(
            NotionDocument(
                page_id=page_id,
                title=meta.title,
                url=meta.url,
                breadcrumb=list(crumb),
                depth=depth,
                last_edited_time=meta.last_edited_time,
                status=status,
                text=cleaned.text if status in ("new", "changed") else None,
                content_hash=digest,
                char_count=len(cleaned.text),
                unknown_count=cleaned.unknown_count,
                unknown_by_type=dict(cleaned.unknown_by_type),
                truncated=fetched.truncated,
                partial=partial,
                table_count=cleaned.table_count,
                sensitive_hits=scan_sensitive(raw),
            )
        )
        seen.add(page_id)

        for database in cleaned.databases:
            databases.setdefault(database.page_id, database)
        for child in cleaned.children:
            pair = (child.page_id, page_id)
            if child.page_id in seen or pair in queued_pairs:
                continue
            if depth + 1 > max_depth:
                reasons.add("max_depth")
                continue
            queued_pairs.add(pair)
            queue.append((child.page_id, crumb + [meta.title], depth + 1, page_id))

    result.databases_skipped = list(databases.values())
    result.incomplete_reasons = sorted(reasons)
    # 삭제 후보는 크롤이 완전했을 때만 내보낸다. 일부만 수집된 목록으로 삭제하면 멀쩡한 문서가 지워진다.
    if result.complete:
        result.deletable_ids = [page_id for page_id in previous if page_id not in seen]
    return result
