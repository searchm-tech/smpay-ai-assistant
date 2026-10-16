import hashlib
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

SECRET = "s" * 40
# 입력 텍스트가 로그/오류 응답에 새어 나가는지 확인하기 위한 표식
SENTINEL = "ZZ-민감-문서-본문-ZZ"


class FakeService:
    """테스트용 가짜 임베딩 서비스. 모델을 로딩하지 않아 빠르다."""

    model_name = "nlpai-lab/KURE-v1"
    revision = "fakerev"
    dim = 8
    max_tokens = 512

    def __init__(self, gate: threading.Event | None = None, fail: bool = False):
        self.gate = gate
        self.fail = fail
        self.entered = threading.Event()

    @staticmethod
    def vector_for(text: str) -> np.ndarray:
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        v = np.random.default_rng(seed).normal(size=FakeService.dim).astype(np.float32)
        return v / np.linalg.norm(v)

    def embed(self, texts: list[str]):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            raise RuntimeError("boom " + " ".join(texts))  # 메시지에 본문이 섞여 있어도 새면 안 됨
        vecs = np.stack([self.vector_for(t) for t in texts])
        truncated = [len(t) > 100 for t in texts]
        return vecs, truncated


def make_settings(**kw) -> Settings:
    return Settings(secret=kw.pop("secret", SECRET), **kw)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    return TestClient(create_app(service=FakeService(), settings=make_settings()))


@pytest.fixture
def auth():
    return {"x-internal-secret": SECRET}
