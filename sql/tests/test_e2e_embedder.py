"""A3 임베딩 서비스(실제 KURE-v1)로 만든 벡터를 DB에 넣고 검색하는 end-to-end 테스트.

필요: 실행 중인 임베딩 서비스 + 환경변수 EMBEDDER_URL(예: http://127.0.0.1:8001), EMBEDDER_SECRET
"""

import os

import httpx
import pytest

from tests.conftest import chunk, match, replace

pytestmark = [pytest.mark.db, pytest.mark.e2e]


def embed(url: str, secret: str, texts: list[str]) -> dict:
    r = httpx.post(f"{url}/embed", json={"texts": texts}, headers={"x-internal-secret": secret}, timeout=120)
    r.raise_for_status()
    return r.json()


def test_real_kure_vectors_round_trip_through_the_database(db):
    url, secret = os.environ.get("EMBEDDER_URL"), os.environ.get("EMBEDDER_SECRET")
    if not url or not secret:
        pytest.skip("EMBEDDER_URL / EMBEDDER_SECRET 이 없어 건너뜀")

    texts = [
        "광고비 충전 한도 변경 방법과 신청 절차를 안내합니다.",
        "충전 한도 증액은 관리자 승인 후 반영됩니다.",
        "이번 주 사무실 점심 메뉴로 추천하는 맛집 목록입니다.",
    ]
    body = embed(url, secret, texts)
    assert body["dim"] == 1024

    chunks = [
        chunk(i, body["embeddings"][i], content=t, revision=body["revision"], model=body["model"])
        for i, t in enumerate(texts)
    ]
    replace(db.ingest, "e2e", chunks)

    query = embed(url, secret, ["광고비 충전 한도를 늘리려면 어떻게 해야 하나요?"])
    rows = match(db.chat, query["embeddings"][0], revision=body["revision"])

    assert [r[2] for r in rows][-1] == texts[2]  # 무관한 문서가 맨 아래
    assert {r[2] for r in rows[:2]} == set(texts[:2])
    assert rows[0][3] > rows[-1][3] + 0.1
