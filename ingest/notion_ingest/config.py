from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from notion_ingest.ids import normalize_page_id

REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """설정 오류. 메시지에는 변수 이름과 위치만 담고 값은 절대 담지 않는다."""


def clean_env_value(value: str) -> str:
    """환경변수에 섞이기 쉬운 앞뒤 공백과 감싼 따옴표를 정리한다."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def load_env() -> None:
    """레포 루트의 .env(git 제외)를 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    load_dotenv(REPO_ROOT / ".env", override=False)


def parse_root_ids(raw: str) -> tuple[str, ...]:
    """쉼표·공백·줄바꿈·세미콜론으로 구분된 페이지 ID/URL 목록을 정규화한다. 비어 있으면 거부(fail-closed)."""
    entries = [e for e in (clean_env_value(p) for p in re.split(r"[,\s;]+", raw or "")) if e]
    if not entries:
        raise ConfigError("NOTION_ROOT_PAGE_IDS must list at least one root page (refusing to crawl the whole workspace)")
    ids: list[str] = []
    for index, entry in enumerate(entries, start=1):
        page_id = normalize_page_id(entry)
        if page_id is None:
            raise ConfigError(f"NOTION_ROOT_PAGE_IDS entry #{index} is not a valid Notion page id or URL")
        if page_id not in ids:
            ids.append(page_id)
    return tuple(ids)


def _number(env: Mapping[str, str], name: str, default: str, cast, positive_what: str):
    raw = clean_env_value(env.get(name, default))
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be {positive_what}") from None
    if not (value > 0 and math.isfinite(value)):
        raise ConfigError(f"{name} must be {positive_what}")
    return value


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)  # repr=False: 오류 메시지·로그에 토큰이 찍히지 않도록
    root_page_ids: tuple[str, ...]
    rate_per_sec: float = 2.5
    max_pages: int = 5000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        token = clean_env_value(env.get("NOTION_TOKEN", ""))
        if not token:
            raise ConfigError("NOTION_TOKEN is not set")
        return cls(
            token=token,
            root_page_ids=parse_root_ids(env.get("NOTION_ROOT_PAGE_IDS", "")),
            rate_per_sec=_number(env, "NOTION_RATE_PER_SEC", "2.5", float, "a positive number"),
            max_pages=_number(env, "NOTION_MAX_PAGES", "5000", int, "a positive integer"),
        )


MIN_EMBEDDER_SECRET_LENGTH = 32  # 임베딩 서버(embedder)가 기동 시 요구하는 최소 길이와 같다
DEFAULT_EMBEDDER_URL = "http://127.0.0.1:8001"


def writer_dsn(environ: Mapping[str, str]) -> str:
    """ingest_writer 접속 문자열. INGEST_DATABASE_URL 이 있으면 그대로 쓰고, 없으면 개발 편의상
    DATABASE_ADMIN_URL 의 호스트에 INGEST_WRITER_PASSWORD 로 접속한다(Supabase 풀러는 사용자명이 `역할.프로젝트ref`).
    운영(회사 데스크탑)에서는 관리자 URL을 두지 말고 INGEST_DATABASE_URL 만 둔다. 동기화는 세션 잠금을 쓰므로 풀러는 세션 모드(5432)."""
    direct = clean_env_value(environ.get("INGEST_DATABASE_URL", ""))
    if direct:
        return direct
    admin = clean_env_value(environ.get("DATABASE_ADMIN_URL", ""))
    password = clean_env_value(environ.get("INGEST_WRITER_PASSWORD", ""))
    if not admin or not password:
        raise ConfigError("set INGEST_DATABASE_URL (or DATABASE_ADMIN_URL together with INGEST_WRITER_PASSWORD)")
    user = conninfo_to_dict(admin).get("user", "")
    project_ref = user.split(".", 1)[1] if "." in user else None
    return make_conninfo(admin, user=f"ingest_writer.{project_ref}" if project_ref else "ingest_writer", password=password)


def parse_allow_list(raw: str) -> frozenset[str]:
    """색인을 허용할 민감 문서(고위험 패턴이 보여도 사람이 확인한 문서)의 페이지 ID 목록. 비어 있어도 된다."""
    entries = [e for e in (clean_env_value(p) for p in re.split(r"[,\s;]+", raw or "")) if e]
    ids: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        page_id = normalize_page_id(entry)
        if page_id is None:
            raise ConfigError(f"INGEST_ALLOW_SENSITIVE_PAGE_IDS entry #{index} is not a valid Notion page id or URL")
        ids.add(page_id)
    return frozenset(ids)


@dataclass(frozen=True)
class SyncSettings:
    notion: Settings
    database_url: str = field(repr=False)  # 비밀번호 포함
    embedder_secret: str = field(repr=False)
    embedder_url: str = DEFAULT_EMBEDDER_URL
    allow_sensitive: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SyncSettings":
        env = os.environ if environ is None else environ
        secret = clean_env_value(env.get("EMBEDDER_SECRET", ""))
        if len(secret) < MIN_EMBEDDER_SECRET_LENGTH:
            raise ConfigError(f"EMBEDDER_SECRET must be set and at least {MIN_EMBEDDER_SECRET_LENGTH} characters long")
        url = clean_env_value(env.get("EMBEDDER_URL", "")) or DEFAULT_EMBEDDER_URL
        if not re.fullmatch(r"https?://[^\s/]+(?::\d+)?(?:/\S*)?", url):
            raise ConfigError("EMBEDDER_URL must be an http(s) URL")
        return cls(
            notion=Settings.from_env(env),
            database_url=writer_dsn(env),
            embedder_secret=secret,
            embedder_url=url,
            allow_sensitive=parse_allow_list(env.get("INGEST_ALLOW_SENSITIVE_PAGE_IDS", "")),
        )
