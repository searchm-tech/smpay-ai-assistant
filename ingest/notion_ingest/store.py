"""Supabase(Postgres)에 쓰는 계층. ingest_writer 역할로 접속해 함수만 호출한다(테이블을 직접 읽거나 쓰지 못한다).

오류 메시지·로그에는 오류 종류와 SQLSTATE만 담는다(문서 본문·제목·비밀값은 담지 않는다).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timezone
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from notion_ingest.timeutil import normalize_time, parse_time

logger = logging.getLogger("notion_ingest")

ADVISORY_LOCK_KEY = 727_274_002  # 동시에 두 곳(맥북 테스트·데스크탑 운영)에서 동기화하지 못하게 하는 잠금


class StoreError(RuntimeError):
    def __init__(self, kind: str, sqlstate: str | None = None):
        super().__init__(f"store failed: kind={kind} sqlstate={sqlstate}")
        self.kind = kind
        self.sqlstate = sqlstate


@dataclass(frozen=True)
class StoredDoc:
    last_edited_time: str | None
    content_hash: str
    title: str
    breadcrumb: str | None
    index_signature: str | None


class Store(Protocol):
    def try_lock(self) -> bool: ...
    def start_run(self) -> str: ...
    def get_sync_state(self) -> dict[str, StoredDoc]: ...
    def replace_document(
        self, *, page_id: str, title: str, url: str, breadcrumb: str | None, last_edited_time: str | None,
        content_hash: str, index_signature: str, chunks: list[dict],
    ) -> None: ...
    def update_metadata(
        self, *, page_id: str, title: str, url: str, breadcrumb: str | None, last_edited_time: str | None
    ) -> bool: ...
    def delete_document(self, page_id: str) -> bool: ...
    def delete_documents_not_in(self, keep_page_ids: list[str]) -> int: ...
    def finish_run(self, run_id: str, status: str, counts: dict[str, int], reasons: list[str]) -> None: ...
    def close(self) -> None: ...


def _wrap(error: psycopg.Error) -> StoreError:
    return StoreError(type(error).__name__, getattr(error, "sqlstate", None))


class PgStore:
    def __init__(self, dsn: str, *, connect_timeout: int = 15) -> None:
        try:
            # prepare_threshold=None: 풀러(트랜잭션 모드)에서도 안전하도록 서버 측 prepared statement 를 쓰지 않는다
            self._conn = psycopg.connect(dsn, autocommit=True, connect_timeout=connect_timeout, prepare_threshold=None)
            self._lock_conn: psycopg.Connection | None = None
            self._dsn, self._timeout = dsn, connect_timeout
        except psycopg.Error as e:
            raise _wrap(e) from None

    def __repr__(self) -> str:  # 접속 문자열(비밀번호 포함)이 repr 에 나오지 않게
        return "PgStore()"

    def close(self) -> None:
        for conn in (self._lock_conn, self._conn):
            if conn is not None:
                try:
                    conn.close()
                except psycopg.Error:
                    pass

    def try_lock(self) -> bool:
        """세션 단위 advisory lock. 전용 연결을 열어 둔 채로 유지하며, 프로세스가 죽으면 서버가 자동으로 푼다.
        세션 잠금이므로 풀러는 세션 모드(5432)여야 한다."""
        try:
            self._lock_conn = psycopg.connect(
                self._dsn, autocommit=True, connect_timeout=self._timeout, prepare_threshold=None
            )
            return bool(self._lock_conn.execute("select pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,)).fetchone()[0])
        except psycopg.Error as e:
            raise _wrap(e) from None

    def start_run(self) -> str:
        try:
            return str(self._conn.execute("select public.start_sync_run()").fetchone()[0])
        except psycopg.Error as e:
            raise _wrap(e) from None

    def get_sync_state(self) -> dict[str, StoredDoc]:
        try:
            rows = self._conn.execute(
                "select notion_page_id, notion_last_edited_at, content_hash, title, breadcrumb, index_signature "
                "from public.get_sync_state()"
            ).fetchall()
        except psycopg.Error as e:
            raise _wrap(e) from None
        state: dict[str, StoredDoc] = {}
        for page_id, edited, content_hash, title, breadcrumb, signature in rows:
            edited_text = edited.astimezone(timezone.utc).isoformat(timespec="milliseconds") if edited else None
            state[page_id] = StoredDoc(normalize_time(edited_text), content_hash, title, breadcrumb, signature)
        return state

    def replace_document(
        self, *, page_id, title, url, breadcrumb, last_edited_time, content_hash, index_signature, chunks
    ) -> None:
        try:
            self._conn.execute(
                "select public.replace_document(%s, %s, %s, %s, %s, %s, %s, %s)",
                (page_id, title, url, breadcrumb, parse_time(last_edited_time), content_hash, index_signature, Jsonb(chunks)),
            )
        except psycopg.Error as e:
            raise _wrap(e) from None

    def update_metadata(self, *, page_id, title, url, breadcrumb, last_edited_time) -> bool:
        try:
            return bool(self._conn.execute(
                "select public.update_document_metadata(%s, %s, %s, %s, %s)",
                (page_id, title, url, breadcrumb, parse_time(last_edited_time)),
            ).fetchone()[0])
        except psycopg.Error as e:
            raise _wrap(e) from None

    def delete_document(self, page_id: str) -> bool:
        try:
            return bool(self._conn.execute("select public.delete_document(%s)", (page_id,)).fetchone()[0])
        except psycopg.Error as e:
            raise _wrap(e) from None

    def delete_documents_not_in(self, keep_page_ids: list[str]) -> int:
        try:
            return int(self._conn.execute("select public.delete_documents_not_in(%s)", (keep_page_ids,)).fetchone()[0])
        except psycopg.Error as e:
            raise _wrap(e) from None

    def finish_run(self, run_id: str, status: str, counts: dict[str, int], reasons: list[str]) -> None:
        try:
            self._conn.execute(
                "select public.finish_sync_run(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id, status, counts.get("new", 0), counts.get("changed", 0), counts.get("metadata_only", 0),
                    counts.get("unchanged", 0), counts.get("deleted", 0), counts.get("quarantined", 0),
                    counts.get("failed", 0), reasons,
                ),
            )
        except psycopg.Error as e:
            raise _wrap(e) from None
