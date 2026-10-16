"""실제 KURE-v1 모델을 쓰는 느린 테스트. RUN_SLOW=1 일 때만 실행한다.

  RUN_SLOW=1 .venv/bin/python -m pytest tests/test_service_real.py -v
"""

import os

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import SECRET, make_settings

pytestmark = pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="RUN_SLOW=1 에서만 실행")


@pytest.fixture(scope="module")
def real_client():
    from app.service import KureEmbeddingService

    service = KureEmbeddingService.load()
    return TestClient(create_app(service=service, settings=make_settings()))


def test_real_model_dim_similarity_and_truncation(real_client):
    headers = {"x-internal-secret": SECRET}
    texts = [
        "광고비 충전 한도를 늘리려면 어떻게 해야 하나요?",
        "광고비 충전 한도 변경 방법과 신청 절차를 안내합니다.",
        "이번 주 사무실 점심 메뉴로 추천하는 맛집 목록입니다.",
        "충전 한도 안내 문장입니다. " * 250,  # 512토큰 초과
    ]
    r = real_client.post("/embed", json={"texts": texts}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    emb = np.array(body["embeddings"])
    assert body["dim"] == 1024 and emb.shape == (4, 1024)
    assert body["revision"] == "8b418a58414668e75532ed045c22d9ca018ae2b2"
    assert float(emb[0] @ emb[1]) > float(emb[0] @ emb[2]) + 0.1  # 관련 > 무관
    assert body["truncated"] == [False, False, False, True]
