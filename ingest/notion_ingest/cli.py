from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Callable, Mapping

from notion_ingest.config import ConfigError, Settings, load_env
from notion_ingest.crawler import crawl
from notion_ingest.notion_client import NotionAuthError, NotionClient, NotionError
from notion_ingest.report import build_report, format_report


def _default_client(settings: Settings) -> NotionClient:
    return NotionClient(settings.token, rate_per_sec=settings.rate_per_sec)


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[Settings], object] | None = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
) -> int:
    parser = argparse.ArgumentParser(prog="notion_ingest", description="Notion 문서 수집")
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure", help="허용 목록 아래를 크롤해 본문 없는 측정 리포트를 출력(DB 쓰기 없음)")
    measure.add_argument("--json", action="store_true", help="JSON으로 출력")
    measure.add_argument("--max-pages", type=int, help="수집할 최대 페이지 수(설정값 덮어쓰기)")
    measure.add_argument("--no-titles", action="store_true", help="리포트에서 제목·URL 제외")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    if environ is None:
        load_env()
    try:
        settings = Settings.from_env(environ if environ is not None else os.environ)
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
