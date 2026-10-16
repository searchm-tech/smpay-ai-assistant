"""합성 벡터 2만 건으로 정확 검색(인덱스 없음) 지연을 잰다. 무겁기 때문에 RUN_PERF=1 일 때만 실행.

  RUN_PERF=1 .venv/bin/python -m pytest tests/test_db_perf.py -s
"""

import os
import random
import statistics
import time

import pytest

from tests.conftest import MODEL, REVISION, TEST_PREFIX, match

pytestmark = [
    pytest.mark.db,
    pytest.mark.perf,
    pytest.mark.skipif(os.environ.get("RUN_PERF") != "1", reason="RUN_PERF=1 에서만 실행"),
]

N_CHUNKS = 20_000
BATCH = 2_000


def test_exact_search_latency_with_20k_chunks(db):
    doc_id = db.admin.execute(
        "insert into public.documents (notion_page_id, title, url, content_hash) "
        "values (%s, 'perf', 'https://example.com/perf', 'h') returning id",
        (TEST_PREFIX + "perf",),
    ).fetchone()[0]

    for start in range(1, N_CHUNKS + 1, BATCH):
        db.admin.execute(
            """
            insert into public.chunks
              (document_id, chunk_index, content, token_count, embedding, embedding_model, embedding_revision)
            select %s, g, 'x', 1,
                   (select array_agg(random()::real) from generate_series(1, 1024) s where g is not null)::extensions.vector(1024),
                   %s, %s
            from generate_series(%s::int, %s::int) g
            """,
            (doc_id, MODEL, REVISION, start, min(start + BATCH - 1, N_CHUNKS)),
        )

    q = [random.gauss(0, 1) for _ in range(1024)]
    norm = sum(x * x for x in q) ** 0.5
    q = [x / norm for x in q]

    match(db.chat, q)  # 워밍업
    times_ms = []
    for _ in range(7):
        t = time.perf_counter()
        rows = match(db.chat, q)
        times_ms.append((time.perf_counter() - t) * 1000)
    assert len(rows) == 8

    plan = db.admin.execute(
        "explain (analyze, format json) select * from public.match_chunks(%s::extensions.vector, %s, 8, null)",
        ("[" + ",".join(f"{x:.6f}" for x in q) + "]", REVISION),
    ).fetchone()[0][0]
    print(
        f"\n[PERF] {N_CHUNKS}청크 정확검색 — 왕복 중앙값 {statistics.median(times_ms):.0f}ms "
        f"(최소 {min(times_ms):.0f} / 최대 {max(times_ms):.0f}), DB 내부 실행 {plan['Execution Time']:.0f}ms"
    )
    # chat_reader의 statement_timeout(5초) 안에 끝나야 한다. 이보다 느리면 인덱스를 도입한다.
    assert statistics.median(times_ms) < 2000
