import json
import logging

import httpx
import pytest

from notion_ingest.embed_client import (
    EmbedAuthError,
    EmbedClient,
    EmbedError,
    EmbedRevisionChanged,
    EmbedUnavailable,
)
from tests.fixtures import SENTINEL

SECRET = "e" * 40
REV = "rev-A"
DIM = 1024


class FakeEmbedder:
    """/embed 대역. 벡터의 첫 성분에 '전역 입력 순번'을 심어 순서 보존을 확인한다."""

    def __init__(self):
        self.requests: list[list[str]] = []
        self.headers: list[dict] = []
        self.script: list = []  # 호출마다 소비할 응답(없으면 정상 응답)
        self.revision = REV
        self.seen = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(dict(request.headers))
        texts = json.loads(request.content)["texts"]
        self.requests.append(texts)
        if self.script:
            action = self.script.pop(0)
            if isinstance(action, Exception):
                raise action
            if isinstance(action, httpx.Response):
                return action
        vectors = []
        for _ in texts:
            v = [0.0] * DIM
            v[0] = float(self.seen)
            vectors.append(v)
            self.seen += 1
        return httpx.Response(200, json={
            "model": "nlpai-lab/KURE-v1", "revision": self.revision, "dim": DIM, "embeddings": vectors,
            "truncated": [t.endswith("!긴") for t in texts], "token_counts": [len(t) + 1 for t in texts],
        })

    def client(self, **kw) -> tuple[EmbedClient, list[float]]:
        sleeps: list[float] = []
        client = EmbedClient(
            "http://embedder.test", SECRET, http=httpx.Client(transport=httpx.MockTransport(self.handler)),
            sleep=sleeps.append, **kw,
        )
        return client, sleeps


@pytest.fixture
def server():
    return FakeEmbedder()


# ---------- 배치와 순서 ----------

def test_texts_are_sent_in_batches_of_at_most_16_and_order_is_preserved(server):
    client, _ = server.client()
    texts = [f"문장 {i}" for i in range(40)]
    batch = client.embed(texts)
    assert [len(r) for r in server.requests] == [16, 16, 8]
    assert [r for chunk in server.requests for r in chunk] == texts
    assert [int(v[0]) for v in batch.vectors] == list(range(40))
    assert len(batch.token_counts) == len(batch.truncated) == 40
    assert batch.token_counts[3] == len("문장 3") + 1


def test_an_empty_input_makes_no_request(server):
    client, _ = server.client()
    assert client.embed([]).vectors == [] and server.requests == []


def test_the_secret_header_is_sent_and_truncation_flags_come_back(server):
    client, _ = server.client()
    batch = client.embed(["일반", "아주!긴"])
    assert server.headers[0]["x-internal-secret"] == SECRET
    assert batch.truncated == [False, True]
    assert batch.revision == REV and batch.model == "nlpai-lab/KURE-v1"


# ---------- 재시도 ----------

def test_a_503_waits_for_retry_after_and_then_succeeds(server):
    server.script = [httpx.Response(503, headers={"Retry-After": "2"}, json={"detail": "busy"})]
    client, sleeps = server.client()
    assert len(client.embed(["a"]).vectors) == 1
    assert sleeps == [2.0] and len(server.requests) == 2


def test_a_persistent_503_ends_as_unavailable_after_the_retry_limit(server):
    server.script = [httpx.Response(503, headers={"Retry-After": "1"}, json={})] * 10
    client, sleeps = server.client(max_retries=3)
    with pytest.raises(EmbedUnavailable) as e:
        client.embed(["a"])
    assert e.value.kind == "busy" and len(server.requests) == 4 and len(sleeps) == 3


def test_network_errors_back_off_and_end_as_unavailable(server):
    server.script = [httpx.ConnectError("refused http://embedder.test")] * 10
    client, sleeps = server.client(max_retries=2)
    with pytest.raises(EmbedUnavailable) as e:
        client.embed(["a"])
    assert e.value.kind == "network" and len(sleeps) == 2 and sleeps[1] > sleeps[0]
    assert "embedder.test" not in str(e.value)


def test_a_transient_network_error_recovers(server):
    server.script = [httpx.ReadTimeout("slow")]
    client, _ = server.client()
    assert len(client.embed(["a"]).vectors) == 1


def test_auth_failures_are_not_retried(server):
    server.script = [httpx.Response(401, json={"detail": "unauthorized"})] * 5
    client, sleeps = server.client()
    with pytest.raises(EmbedAuthError):
        client.embed(["a"])
    assert len(server.requests) == 1 and sleeps == []


def test_client_errors_such_as_422_are_not_retried(server):
    server.script = [httpx.Response(422, json={"detail": []})] * 5
    client, _ = server.client()
    with pytest.raises(EmbedError) as e:
        client.embed(["a"])
    assert e.value.kind == "bad_request" and len(server.requests) == 1


# ---------- 일관성 ----------

def test_a_revision_change_between_batches_aborts(server):
    client, _ = server.client()
    texts = [f"t{i}" for i in range(20)]
    orig = server.handler

    def switching(request):
        response = orig(request)
        if len(server.requests) == 1:
            server.revision = "rev-B"
        return response

    client._http = httpx.Client(transport=httpx.MockTransport(switching))
    with pytest.raises(EmbedRevisionChanged):
        client.embed(texts)


def test_a_revision_change_between_calls_aborts(server):
    client, _ = server.client()
    client.embed(["a"])
    server.revision = "rev-B"
    with pytest.raises(EmbedRevisionChanged):
        client.embed(["b"])


def test_probe_reports_the_serving_model_and_revision(server):
    client, _ = server.client()
    assert client.probe() == ("nlpai-lab/KURE-v1", REV)


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(embeddings=b["embeddings"][:-1]),  # 개수 불일치
    lambda b: b.update(embeddings=[[0.0] * 8] * len(b["embeddings"]), dim=8),  # 차원 불일치
    lambda b: b.pop("token_counts"),  # 필드 누락
    lambda b: b.update(token_counts=[1]),  # 길이 불일치
])
def test_malformed_responses_are_rejected(server, mutate):
    good = server.handler(httpx.Request("POST", "http://x", content=json.dumps({"texts": ["a", "b"]})))
    body = json.loads(good.content)
    mutate(body)
    server.requests.clear()
    server.script = [httpx.Response(200, json=body)]
    client, _ = server.client()
    with pytest.raises(EmbedError) as e:
        client.embed(["a", "b"])
    assert e.value.kind == "bad_response"


# ---------- 비밀·본문 노출 방지 ----------

@pytest.mark.parametrize("status", [400, 422, 500, 501, 502])
def test_errors_and_logs_never_contain_the_texts_or_the_secret(server, caplog, status):
    server.script = [httpx.Response(status, json={"detail": "boom " + SENTINEL + SECRET})] * 10
    client, _ = server.client(max_retries=1)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(EmbedError) as e:
        client.embed([SENTINEL])
    blob = str(e.value) + repr(e.value) + caplog.text + repr(client)
    assert SENTINEL not in blob and SECRET not in blob
