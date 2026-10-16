import asyncio
import logging

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings
from tests.conftest import SECRET, SENTINEL, FakeService, make_settings


# ---------- 1. 인증 (실패 케이스 먼저) ----------

def test_missing_secret_returns_401(client):
    r = client.post("/embed", json={"texts": ["안녕"]})
    assert r.status_code == 401


def test_wrong_secret_returns_401(client):
    r = client.post("/embed", json={"texts": ["안녕"]}, headers={"x-internal-secret": "x" * 40})
    assert r.status_code == 401


def test_401_body_is_identical_for_missing_and_wrong_and_leaks_nothing(client):
    missing = client.post("/embed", json={"texts": ["a"]})
    wrong = client.post("/embed", json={"texts": ["a"]}, headers={"x-internal-secret": "nope"})
    assert missing.json() == wrong.json() == {"detail": "unauthorized"}
    assert SECRET not in missing.text + wrong.text


def test_auth_is_checked_before_body_validation(client):
    r = client.post("/embed", json={"wrong": "shape"})  # 인증 없음 + 잘못된 본문
    assert r.status_code == 401  # 422로 내용을 알려주면 안 됨


def test_startup_fails_without_secret():
    with pytest.raises(RuntimeError):
        with TestClient(create_app(settings=Settings(secret=""))):
            pass


def test_startup_fails_with_short_secret():
    with pytest.raises(RuntimeError) as e:
        with TestClient(create_app(settings=Settings(secret="short-secret"))):
            pass
    assert "short-secret" not in str(e.value)  # 값 자체를 메시지에 남기지 않음


def test_settings_from_env_strips_whitespace_and_quotes(monkeypatch):
    monkeypatch.setenv("EMBEDDER_SECRET", f'  "{SECRET}"  ')
    assert Settings.from_env().secret == SECRET


def test_server_starts_with_valid_secret_and_injected_service():
    with TestClient(create_app(service=FakeService(), settings=make_settings())) as c:
        assert c.get("/health").status_code == 200


# ---------- 2. 입력 검증 ----------

@pytest.mark.parametrize(
    "body",
    [
        {"texts": []},
        {"texts": ["a"] * 33},
        {"texts": ["a", 123]},
        {"texts": "문자열"},
        {"texts": ["가" * 4001]},
        {"texts": ["a"], "unknown": 1},
        {},
    ],
    ids=["empty", "33개", "문자열아닌값", "리스트아님", "4001자", "알수없는필드", "texts없음"],
)
def test_invalid_input_returns_422(client, auth, body):
    assert client.post("/embed", json=body, headers=auth).status_code == 422


def test_boundary_values_are_accepted(client, auth):
    r = client.post("/embed", json={"texts": ["가" * 4000] + ["a"] * 31}, headers=auth)
    assert r.status_code == 200
    assert len(r.json()["embeddings"]) == 32


def test_422_response_and_logs_do_not_echo_input(client, auth, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.post("/embed", json={"texts": [SENTINEL * 400], "evil": SENTINEL}, headers=auth)
    assert r.status_code == 422
    assert SENTINEL not in r.text
    assert SENTINEL not in caplog.text


def test_oversized_body_returns_413(client, auth):
    r = client.post(
        "/embed",
        content=b"x" * (1024 * 1024 + 1),
        headers={**auth, "content-type": "application/json"},
    )
    assert r.status_code == 413


def test_missing_content_length_returns_411(client, auth):
    def chunks():
        yield b'{"texts": ["a"]}'

    r = client.post("/embed", content=chunks(), headers={**auth, "content-type": "application/json"})
    assert r.status_code == 411


# ---------- 3. 정상 동작 ----------

def test_response_shape_order_and_normalization(client, auth):
    texts = ["첫째", "둘째", "셋째"]
    r = client.post("/embed", json={"texts": texts}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "nlpai-lab/KURE-v1"
    assert body["revision"] == "fakerev"
    assert body["dim"] == 8
    emb = np.array(body["embeddings"])
    assert emb.shape == (3, 8)
    assert np.allclose(np.linalg.norm(emb, axis=1), 1.0, atol=1e-4)
    for i, t in enumerate(texts):  # 입력 순서 유지
        assert np.allclose(emb[i], FakeService.vector_for(t), atol=1e-5)


def test_token_counts_are_returned_per_input_in_order(client, auth):
    texts = ["가나", "가나다라마바사아", "가"]
    body = client.post("/embed", json={"texts": texts}, headers=auth).json()
    assert body["token_counts"] == [max(1, len(t) // 2) for t in texts]
    assert all(isinstance(n, int) and n >= 1 for n in body["token_counts"])


def test_truncated_flags_are_returned_per_input(client, auth):
    r = client.post("/embed", json={"texts": ["짧음", "가" * 500, "짧음2"]}, headers=auth)
    assert r.status_code == 200
    assert r.json()["truncated"] == [False, True, False]


def test_request_id_header_present(client, auth):
    r = client.post("/embed", json={"texts": ["a"]}, headers=auth)
    assert r.headers.get("x-request-id")


# ---------- 4. health ----------

def test_health_needs_no_auth_and_exposes_minimum(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------- 5. 오류·로그 ----------

def test_internal_error_is_generic_and_leaks_no_input(auth, caplog):
    caplog.set_level(logging.DEBUG)
    c = TestClient(
        create_app(service=FakeService(fail=True), settings=make_settings()),
        raise_server_exceptions=False,
    )
    r = c.post("/embed", json={"texts": [SENTINEL]}, headers=auth)
    assert r.status_code == 500
    assert r.json() == {"detail": "internal error"}
    assert SENTINEL not in r.text
    assert SENTINEL not in caplog.text


def test_logs_have_metadata_but_never_the_text(client, auth, caplog):
    caplog.set_level(logging.INFO, logger="embedder")
    r = client.post("/embed", json={"texts": [SENTINEL]}, headers=auth)
    assert r.status_code == 200
    assert SENTINEL not in caplog.text
    assert "texts=1" in caplog.text
    assert "status=200" in caplog.text


def test_failed_request_releases_capacity(auth):
    # queue_limit=0 → 동시에 1건만 허용. 실패한 요청이 자리를 반납하지 않으면 다음 요청이 503이 된다.
    app = create_app(service=FakeService(fail=True), settings=make_settings(queue_limit=0))
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/embed", json={"texts": ["a"]}, headers=auth).status_code == 500
    assert c.post("/embed", json={"texts": ["a"]}, headers=auth).status_code == 500  # 503이 아니어야 함


# ---------- 6. 동시성: 직렬 처리 + 대기열 상한 ----------

@pytest.mark.anyio
async def test_busy_returns_503_with_retry_after_when_queue_is_full():
    import threading

    gate = threading.Event()
    service = FakeService(gate=gate)
    app = create_app(service=service, settings=make_settings(queue_limit=1))
    headers = {"x-internal-secret": SECRET}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as ac:
        a = asyncio.create_task(ac.post("/embed", json={"texts": ["a"]}, headers=headers))
        await asyncio.to_thread(service.entered.wait, 5)  # A가 모델 안에서 실행 중
        b = asyncio.create_task(ac.post("/embed", json={"texts": ["b"]}, headers=headers))
        await asyncio.sleep(0.1)  # B가 대기열에 들어감

        c = await ac.post("/embed", json={"texts": ["c"]}, headers=headers)  # 대기열 가득 참
        assert c.status_code == 503
        assert c.headers.get("retry-after")

        gate.set()
        ra, rb = await asyncio.gather(a, b)
        assert ra.status_code == 200 and rb.status_code == 200  # 대기하던 요청은 정상 처리
