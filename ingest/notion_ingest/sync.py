"""동기화 오케스트레이터: 크롤 → 청크 → 임베딩 → 문서 단위 원자적 교체 → (조건이 맞을 때만) 삭제 정리.

원칙
- Notion은 읽기만 한다(여기서는 NotionLike.get_page / get_markdown 만 호출).
- 한 문서의 실패가 다른 문서를 막지 않는다. 임베딩 서버 자체가 죽었으면 연속 실패 시 중단한다.
- 삭제는 크롤이 완전하고, 쓰기 실패·미처리 문서가 없고, 대량 삭제 가드를 통과했을 때만 한다.
- 로그·요약에는 문서 id·개수·오류 종류만 남긴다(제목·본문·비밀값 없음).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from notion_ingest.chunker import CHUNKER_VERSION, EMBED_INPUT_VERSION, Chunk, chunk_document, split_in_half
from notion_ingest.crawler import NotionLike, PrevDoc, crawl
from notion_ingest.embed_client import (
    EmbedAuthError,
    EmbedBatch,
    EmbedError,
    EmbedRevisionChanged,
    EmbedUnavailable,
)
from notion_ingest.notion_client import NotionAuthError, NotionError
from notion_ingest.store import Store, StoreError

logger = logging.getLogger("notion_ingest")

# 기본은 색인하지 않는 고위험 패턴(주민번호·카드번호·API 키 모양·비밀번호 대입). 이메일·전화번호 등은 리포트에만 센다.
HIGH_RISK_PATTERNS = frozenset({"rrn", "card", "api_key_like", "secret_keyword"})
COUNT_KEYS = ("new", "changed", "metadata_only", "unchanged", "deleted", "quarantined", "failed")
FATAL_REASONS = frozenset({
    "embedder_auth", "embedder_unavailable", "revision_changed", "notion_auth", "notion_error",
    "store_error", "locked", "interrupted", "internal_error",
})
MASS_DELETE_ABSOLUTE = 10  # 삭제가 이 개수(또는 전체의 20%)를 넘으면 --force-delete 없이는 거부
MASS_DELETE_RATIO = 0.2


class Embedder(Protocol):
    def probe(self) -> tuple[str, str]: ...
    def embed(self, texts: list[str]) -> EmbedBatch: ...


def make_signature(revision: str) -> str:
    """색인 서명 = 임베딩 모델 revision + 청크 규칙 버전 + 임베딩 입력 방식. 바뀌면 모든 문서를 재색인한다."""
    return f"{revision}|chunker{CHUNKER_VERSION}|input{EMBED_INPUT_VERSION}"


@dataclass(frozen=True)
class SyncOptions:
    dry_run: bool = False
    limit: int | None = None  # 이번 실행에서 임베딩할 new/changed 문서 수 상한(나머지는 다음 실행으로)
    force_reindex: bool = False
    force_delete: bool = False
    allow_sensitive: frozenset[str] = frozenset()
    max_pages: int = 5000
    max_split_rounds: int = 2  # truncated 청크를 반으로 나누는 최대 횟수
    max_consecutive_embed_failures: int = 3


@dataclass
class SyncSummary:
    status: str = "complete"  # complete | partial | failed
    exit_code: int = 0
    counts: dict[str, int] = field(default_factory=lambda: {key: 0 for key in COUNT_KEYS})
    reasons: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (page_id, 오류 종류)
    quarantined: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)  # (page_id, 패턴 이름)
    deferred: int = 0
    partial_docs: int = 0
    estimated_chunks: int = 0
    delete_candidates: int = 0
    dry_run: bool = False
    run_id: str | None = None

    def add_reason(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)

    def finalize(self) -> "SyncSummary":
        problems = [r for r in self.reasons if r != "limited"]
        self.status = "failed" if FATAL_REASONS & set(self.reasons) else ("partial" if self.reasons else "complete")
        self.exit_code = 1 if problems else 0
        return self


@dataclass
class _Piece:
    path: str
    content: str


@dataclass
class _Record:
    path: str
    content: str
    tokens: int
    truncated: bool
    vector: list[float]


def _embed_pieces(embedder: Embedder, pieces: list[_Piece], rounds_left: int, revision: str):
    batch = embedder.embed([Chunk(0, p.path, p.content).embed_text for p in pieces])
    if batch.revision != revision:  # 클라이언트도 막지만, 한 문서의 청크는 모두 같은 revision 이어야 하므로 한 번 더 확인
        raise EmbedRevisionChanged("revision_changed", 200)
    records: list[_Record] = []
    for piece, vector, truncated, tokens in zip(pieces, batch.vectors, batch.truncated, batch.token_counts):
        if truncated and rounds_left > 0:
            halves = split_in_half(piece.content)
            if len(halves) == 2:
                records += _embed_pieces(embedder, [_Piece(piece.path, h) for h in halves], rounds_left - 1, revision)[0]
                continue
        records.append(_Record(piece.path, piece.content, tokens, truncated, vector))
    return records, batch.model


def _embed_chunks(embedder: Embedder, chunks: list[Chunk], max_split_rounds: int, revision: str):
    if not chunks:
        return [], ""
    return _embed_pieces(embedder, [_Piece(c.heading_path, c.content) for c in chunks], max_split_rounds, revision)


def _high_risk(doc) -> tuple[str, ...]:
    return tuple(sorted(HIGH_RISK_PATTERNS & set(doc.sensitive_hits)))


def run_sync(
    notion: NotionLike,
    embedder: Embedder,
    store: Store,
    root_ids: list[str] | tuple[str, ...],
    options: SyncOptions = SyncOptions(),
) -> SyncSummary:
    summary = SyncSummary(dry_run=options.dry_run)

    # 1) 사전 점검: 임베딩 서버가 안 뜨면 DB·Notion 을 건드리기 전에 중단한다
    try:
        _model, revision = embedder.probe()
    except EmbedAuthError:
        summary.add_reason("embedder_auth")
        return summary.finalize()
    except EmbedError:
        summary.add_reason("embedder_unavailable")
        return summary.finalize()
    signature = make_signature(revision)

    if options.dry_run:
        try:
            _execute(notion, embedder, store, root_ids, options, summary, signature, revision)
        except StoreError:
            summary.add_reason("store_error")
        except NotionAuthError:
            summary.add_reason("notion_auth")
        except NotionError:
            summary.add_reason("notion_error")
        return summary.finalize()

    # 2) 동시 실행 방지 → 실행 기록 시작
    try:
        if not store.try_lock():
            summary.add_reason("locked")
            return summary.finalize()
        summary.run_id = store.start_run()
    except StoreError:
        summary.add_reason("store_error")
        return summary.finalize()

    try:
        _execute(notion, embedder, store, root_ids, options, summary, signature, revision)
    except NotionAuthError:
        summary.add_reason("notion_auth")
    except NotionError:
        summary.add_reason("notion_error")
    except StoreError:
        summary.add_reason("store_error")
    except BaseException as e:  # Ctrl+C 등. 실행 기록을 닫은 뒤 그대로 올려 보낸다
        summary.add_reason("interrupted" if isinstance(e, (KeyboardInterrupt, SystemExit)) else "internal_error")
        _record_finish(store, summary)
        raise
    _record_finish(store, summary.finalize())
    return summary


def _record_finish(store: Store, summary: SyncSummary) -> None:
    summary.finalize()
    try:
        store.finish_run(summary.run_id, summary.status, summary.counts, summary.reasons)
    except StoreError as e:  # 기록 실패가 동기화 결과를 바꾸지는 않는다
        logger.error("could not record sync run kind=%s sqlstate=%s", e.kind, e.sqlstate)


def _execute(notion, embedder, store, root_ids, options, summary: SyncSummary, signature: str, revision: str) -> None:
    state = store.get_sync_state()
    previous = {
        page_id: PrevDoc(s.last_edited_time, s.content_hash, s.title, s.breadcrumb) for page_id, s in state.items()
    }
    stale = set(state) if options.force_reindex else {p for p, s in state.items() if s.index_signature != signature}

    result = crawl(notion, root_ids, previous, max_pages=options.max_pages, reindex_ids=stale)
    for reason in result.incomplete_reasons:
        summary.add_reason(f"crawl_{reason}")
    summary.delete_candidates = len(result.deletable_ids)
    summary.partial_docs = sum(1 for d in result.documents if d.partial or d.truncated)

    keep_ids: list[str] = []
    consecutive_unavailable = 0
    processed = 0
    aborted = False

    def fail(page_id: str, kind: str) -> None:
        summary.counts["failed"] += 1
        summary.failed.append((page_id, kind))
        summary.add_reason("doc_failures")

    for doc in result.documents:
        patterns = _high_risk(doc)
        if patterns and doc.page_id not in options.allow_sensitive:
            summary.counts["quarantined"] += 1
            summary.quarantined.append((doc.page_id, patterns))
            if doc.page_id in state and not options.dry_run:
                try:  # 이미 색인된 문서는 크롤 완전성과 무관하게 즉시 검색에서 뺀다
                    store.delete_document(doc.page_id)
                except StoreError as e:
                    fail(doc.page_id, e.kind)
            continue
        keep_ids.append(doc.page_id)

        if doc.status == "unchanged":
            summary.counts["unchanged"] += 1
            continue
        if doc.status == "metadata_only":
            summary.counts["metadata_only"] += 1
            if not options.dry_run:
                summary.counts["metadata_only"] -= 1
                try:
                    found = store.update_metadata(
                        page_id=doc.page_id, title=doc.title, url=doc.url,
                        breadcrumb=" > ".join(doc.breadcrumb) or None, last_edited_time=doc.last_edited_time,
                    )
                except StoreError as e:
                    fail(doc.page_id, e.kind)
                    continue
                if not found:
                    fail(doc.page_id, "metadata_target_missing")
                    continue
                summary.counts["metadata_only"] += 1
            continue

        # new / changed
        if options.dry_run:
            summary.counts[doc.status] += 1
            summary.estimated_chunks += len(chunk_document(doc.title, doc.text or ""))
            continue
        if options.limit is not None and processed >= options.limit:
            summary.deferred += 1
            summary.add_reason("limited")
            continue
        processed += 1
        try:
            n_chunks = _index_document(embedder, store, doc, options, signature, revision)
        except EmbedAuthError:
            summary.add_reason("embedder_auth")
            aborted = True
            break
        except EmbedRevisionChanged:
            summary.add_reason("revision_changed")
            aborted = True
            break
        except EmbedUnavailable as e:
            fail(doc.page_id, e.kind)
            consecutive_unavailable += 1
            if consecutive_unavailable >= options.max_consecutive_embed_failures:
                summary.add_reason("embedder_unavailable")
                aborted = True
                break
            continue
        except EmbedError as e:
            fail(doc.page_id, e.kind)
            continue
        except StoreError as e:
            fail(doc.page_id, e.kind)
            continue
        consecutive_unavailable = 0
        summary.counts[doc.status] += 1
        logger.info("indexed page_id=%s status=%s chunks=%d", doc.page_id, doc.status, n_chunks)

    if options.dry_run:
        return

    # 삭제 정리: 일부만 본 목록으로 지우면 멀쩡한 문서가 사라지므로, 모든 조건이 맞을 때만 한다
    blocked = (not result.complete) or summary.counts["failed"] > 0 or aborted or summary.deferred > 0
    deletable = result.deletable_ids
    if deletable and not blocked:
        limit_n = max(1, min(MASS_DELETE_ABSOLUTE, int(len(state) * MASS_DELETE_RATIO)))
        if (len(deletable) > limit_n and not options.force_delete) or not keep_ids:
            summary.add_reason("delete_guard")
        else:
            summary.counts["deleted"] = store.delete_documents_not_in(keep_ids)


def _index_document(embedder, store, doc, options, signature: str, revision: str) -> int:
    chunks = chunk_document(doc.title, doc.text or "")
    records, model = _embed_chunks(embedder, chunks, options.max_split_rounds, revision)
    payload = [
        {
            "chunk_index": index,
            "heading_path": r.path,
            "content": r.content,
            "token_count": r.tokens,
            "truncated": r.truncated,
            "embedding": r.vector,
            "embedding_model": model,
            "embedding_revision": revision,
        }
        for index, r in enumerate(records)
    ]
    store.replace_document(
        page_id=doc.page_id,
        title=doc.title,
        url=doc.url,
        breadcrumb=" > ".join(doc.breadcrumb) or None,
        last_edited_time=doc.last_edited_time,
        content_hash=doc.content_hash,
        index_signature=signature,
        chunks=payload,
    )
    return len(payload)
