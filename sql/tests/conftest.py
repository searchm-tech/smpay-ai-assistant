"""실제 Supabase DB에 대한 테스트 공통 설정.

필요: 레포 루트 .env 의 DATABASE_ADMIN_URL, CHAT_READER_PASSWORD, INGEST_WRITER_PASSWORD
(없으면 DB 테스트는 건너뛴다). 테스트 데이터는 notion_page_id 가 `__test__` 로 시작하고 revision 이
`test-rev-1` 이라 실제 데이터와 섞이거나 검색에 걸리지 않으며, 테스트 전후로 지운다.
"""

import os
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.types.json import Jsonb

from dbtools.config import load_env, require_env, role_dsn

load_env()

TEST_PREFIX = "__test__"
REVISION = "test-rev-1"
MODEL = "test-model"
DIM = 1024


# ---------- 헬퍼 ----------

def unit(i: int, dim: int = DIM) -> list[float]:
    v = [0.0] * dim
    v[i] = 1.0
    return v


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def chunk(i: int, vec: list[float], content: str = "내용", revision: str = REVISION, model: str = MODEL) -> dict:
    return {
        "chunk_index": i,
        "heading_path": "제목 > 소제목",
        "content": content,
        "token_count": 5,
        "truncated": False,
        "embedding": vec,
        "embedding_model": model,
        "embedding_revision": revision,
    }


def replace(
    conn, page_id: str, chunks: list[dict], content_hash: str = "h1", title: str = "테스트 문서", signature="test-sig"
):
    return conn.execute(
        "select public.replace_document(%s, %s, %s, %s, %s, %s, %s, %s)",
        (TEST_PREFIX + page_id, title, f"https://example.com/{page_id}", None, None, content_hash, signature, Jsonb(chunks)),
    ).fetchone()[0]


def match(conn, vec: list[float], revision: str = REVISION, limit: int = 8, ids=None):
    return conn.execute(
        "select chunk_id, document_id, content, similarity "
        "from public.match_chunks(%s::extensions.vector, %s, %s, %s)",
        (vec_literal(vec), revision, limit, ids),
    ).fetchall()


def count_docs(admin, page_id: str) -> int:
    return admin.execute(
        "select count(*) from public.documents where notion_page_id = %s", (TEST_PREFIX + page_id,)
    ).fetchone()[0]


def count_chunks(admin, document_id) -> int:
    return admin.execute("select count(*) from public.chunks where document_id = %s", (document_id,)).fetchone()[0]


def _purge(admin) -> None:
    admin.execute("delete from public.documents where starts_with(notion_page_id, %s)", (TEST_PREFIX,))


# ---------- 픽스처 ----------

@pytest.fixture(scope="session")
def dsns():
    needed = ("DATABASE_ADMIN_URL", "CHAT_READER_PASSWORD", "INGEST_WRITER_PASSWORD")
    if not all(os.environ.get(k) for k in needed):
        pytest.skip("DB 환경변수(DATABASE_ADMIN_URL 등)가 없어 건너뜀")
    admin = require_env("DATABASE_ADMIN_URL")
    return {
        "admin": admin,
        "chat": role_dsn(admin, "chat_reader", require_env("CHAT_READER_PASSWORD")),
        "ingest": role_dsn(admin, "ingest_writer", require_env("INGEST_WRITER_PASSWORD")),
    }


@pytest.fixture(scope="session")
def migrated(dsns):
    with psycopg.connect(dsns["admin"], autocommit=True, connect_timeout=15) as conn:
        try:
            applied = conn.execute(
                "select count(*) from public.schema_migrations where version in ('001', '002')"
            ).fetchone()[0] == 2
        except psycopg.errors.UndefinedTable:
            applied = False
    if not applied:
        pytest.skip("마이그레이션 001·002가 적용되지 않음: `python apply_migrations.py` 를 먼저 실행")


@pytest.fixture
def db(dsns, migrated):
    conns = {name: psycopg.connect(dsn, autocommit=True, connect_timeout=15) for name, dsn in dsns.items()}
    admin = conns["admin"]
    _purge(admin)
    try:
        yield SimpleNamespace(admin=admin, chat=conns["chat"], ingest=conns["ingest"])
    finally:
        _purge(admin)
        left = admin.execute(
            "select count(*) from public.documents where starts_with(notion_page_id, %s)", (TEST_PREFIX,)
        ).fetchone()[0]
        for c in conns.values():
            c.close()
        assert left == 0, "테스트 데이터가 정리되지 않음"
