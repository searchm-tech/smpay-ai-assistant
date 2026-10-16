from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Callable, Mapping

from notion_ingest.config import ConfigError, Settings, SyncSettings, load_env
from notion_ingest.crawler import crawl
from notion_ingest.embed_client import EmbedClient
from notion_ingest.notion_client import NotionAuthError, NotionClient, NotionError
from notion_ingest.report import build_report, format_report
from notion_ingest.store import PgStore, StoreError
from notion_ingest.sync import SyncOptions, run_sync
from notion_ingest.sync_report import format_summary, summary_to_dict


def _default_client(settings: Settings) -> NotionClient:
    return NotionClient(settings.token, rate_per_sec=settings.rate_per_sec)


def _default_embedder(settings: SyncSettings) -> EmbedClient:
    return EmbedClient(settings.embedder_url, settings.embedder_secret)


def _default_store(settings: SyncSettings) -> PgStore:
    return PgStore(settings.database_url)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _close(resource) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[Settings], object] | None = None,
    embedder_factory: Callable[[SyncSettings], object] | None = None,
    store_factory: Callable[[SyncSettings], object] | None = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> int:
    parser = argparse.ArgumentParser(prog="notion_ingest", description="Notion 문서 수집")
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure", help="허용 목록 아래를 크롤해 본문 없는 측정 리포트를 출력(DB 쓰기 없음)")
    measure.add_argument("--json", action="store_true", help="JSON으로 출력")
    measure.add_argument("--max-pages", type=int, help="수집할 최대 페이지 수(설정값 덮어쓰기)")
    measure.add_argument("--no-titles", action="store_true", help="리포트에서 제목·URL 제외")
    sync = sub.add_parser("sync", help="Notion → 청크 → 임베딩 → DB 동기화(변경된 문서만 갱신)")
    sync.add_argument("--dry-run", action="store_true", help="계획과 예상 청크 수만 출력(임베딩·DB 쓰기 없음)")
    sync.add_argument("--limit", type=_positive_int, help="이번 실행에서 임베딩할 신규·변경 문서 수 상한(삭제는 건너뜀)")
    sync.add_argument("--force-reindex", action="store_true", help="내용이 같아도 모든 문서를 다시 임베딩")
    sync.add_argument("--force-delete", action="store_true", help="대량 삭제 가드를 무시하고 삭제 정리를 수행")
    sync.add_argument("--json", action="store_true", help="JSON으로 출력")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    if environ is None:
        load_env()
    source = environ if environ is not None else os.environ
    if args.command == "sync":
        return _sync(args, source, client_factory, embedder_factory, store_factory, out, err)
    try:
        settings = Settings.from_env(source)
    except ConfigError as e:
        err(f"설정 오류: {e}")
        return 2
    if args.max_pages:
        settings = dataclasses.replace(settings, max_pages=args.max_pages)

    client = (client_factory or _default_client)(settings)
    try:
        result = crawl(client, settings.root_page_ids, {}, max_pages=settings.max_pages)
    except NotionAuthError as e:
        err(f"Notion 인증 오류 (status={e.status}): 토큰과 통합의 'Read content' 권한을 확인하세요")
        return 1
    except NotionError as e:
        err(f"Notion 오류 (kind={e.kind}, status={e.status})")
        return 1
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    report = build_report(result, include_titles=not args.no_titles)
    out(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_report(report))
    return 0


def _sync(args, source, client_factory, embedder_factory, store_factory, out, err) -> int:
    try:
        settings = SyncSettings.from_env(source)
    except ConfigError as e:
        err(f"설정 오류: {e}")
        return 2

    resources: list = []
    try:
        try:
            store = (store_factory or _default_store)(settings)
        except StoreError as e:
            err(f"DB 연결 실패 (kind={e.kind}, sqlstate={e.sqlstate}): INGEST_DATABASE_URL 과 풀러 모드(세션 5432)를 확인하세요")
            return 1
        resources.append(store)
        client = (client_factory or _default_client)(settings.notion)
        resources.append(client)
        embedder = (embedder_factory or _default_embedder)(settings)
        resources.append(embedder)

        options = SyncOptions(
            dry_run=args.dry_run,
            limit=args.limit,
            force_reindex=args.force_reindex,
            force_delete=args.force_delete,
            allow_sensitive=settings.allow_sensitive,
            max_pages=settings.notion.max_pages,
        )
        summary = run_sync(client, embedder, store, settings.notion.root_page_ids, options)
    finally:
        for resource in reversed(resources):
            _close(resource)

    out(json.dumps(summary_to_dict(summary), ensure_ascii=False, indent=2) if args.json else format_summary(summary))
    return summary.exit_code
