"""임베딩 서비스(embedder, A3)용 HTTP 클라이언트.

오류 메시지·로그에는 종류와 상태코드만 담는다(입력 텍스트, 비밀값, 응답 본문은 담지 않는다).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger("notion_ingest")

BATCH_SIZE = 16  # A2 측정 기준. 서버 상한(32)보다 작게 둔다
EXPECTED_DIM = 1024
RETRYABLE_STATUS = {500, 502, 504}


class EmbedError(RuntimeError):
    def __init__(self, kind: str, status: int | None = None):
        super().__init__(f"embedder request failed: kind={kind} status={status}")
        self.kind = kind
        self.status = status


class EmbedUnavailable(EmbedError):
    """임베딩 서버가 응답하지 못함(꺼짐·과부하·네트워크). 재시도를 소진했다."""


class EmbedAuthError(EmbedError):
    """시크릿이 틀림. 재시도해도 소용없다."""


class EmbedRevisionChanged(EmbedError):
    """실행 중 서버의 모델 revision 이 바뀜. 한 문서의 청크는 모두 같은 revision 이어야 하므로 중단한다."""


@dataclass(frozen=True)
class EmbedBatch:
    vectors: list[list[float]]
    truncated: list[bool]
    token_counts: list[int]
    model: str
    revision: str


class EmbedClient:
    def __init__(
        self,
        url: str,
        secret: str,
        *,
        http: httpx.Client | None = None,
        batch_size: int = BATCH_SIZE,
        max_retries: int = 3,
        timeout: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url.rstrip("/")
        self._headers = {"x-internal-secret": secret}
        self._http = http or httpx.Client(timeout=timeout)
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._sleep = sleep
        self._revision: str | None = None

    def __repr__(self) -> str:  # 시크릿이 든 헤더가 repr 에 나오지 않게
        return "EmbedClient()"

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ 공개 API

    def embed(self, texts: list[str]) -> EmbedBatch:
        vectors: list[list[float]] = []
        truncated: list[bool] = []
        token_counts: list[int] = []
        model = revision = ""
        for start in range(0, len(texts), self._batch_size):
            part = texts[start : start + self._batch_size]
            data = self._post(part)
            vectors += data["embeddings"]
            truncated += data["truncated"]
            token_counts += data["token_counts"]
            model, revision = data["model"], data["revision"]
        return EmbedBatch(vectors, truncated, token_counts, model, revision)

    def probe(self) -> tuple[str, str]:
        """서버가 살아 있고 인증이 맞는지, 어떤 모델·revision 인지 확인한다(DB를 건드리기 전 사전 점검)."""
        batch = self.embed(["preflight"])
        return batch.model, batch.revision

    # ------------------------------------------------------------ 내부

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(30.0, float(2**attempt))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        try:
            return min(30.0, max(0.0, float(response.headers["Retry-After"])))
        except (KeyError, ValueError):
            return None

    def _post(self, texts: list[str]) -> dict:
        attempt = 0
        while True:
            try:
                response = self._http.post(f"{self._url}/embed", json={"texts": texts}, headers=self._headers)
            except httpx.TransportError:  # 원래 메시지에는 URL 등이 섞일 수 있어 버린다
                kind, status, wait = "network", None, self._backoff(attempt)
            else:
                status = response.status_code
                if status == 200:
                    return self._validate(response, len(texts))
                if status in (401, 403):
                    raise EmbedAuthError("unauthorized", status)
                if status in (503, 429):
                    wait = self._retry_after(response)
                    kind, wait = "busy", (wait if wait is not None else self._backoff(attempt))
                elif status in RETRYABLE_STATUS:
                    kind, wait = f"http_{status}", self._backoff(attempt)
                elif 400 <= status < 500:
                    raise EmbedError("bad_request", status)
                else:
                    raise EmbedError(f"http_{status}", status)

            if attempt >= self._max_retries:
                raise EmbedUnavailable(kind, status)
            logger.warning("embedder retry kind=%s attempt=%d wait=%.1fs", kind, attempt + 1, wait)
            self._sleep(wait)
            attempt += 1

    def _validate(self, response: httpx.Response, count: int) -> dict:
        try:
            data = response.json()
            vectors, truncated, counts = data["embeddings"], data["truncated"], data["token_counts"]
            model, revision, dim = data["model"], data["revision"], data["dim"]
        except (ValueError, KeyError, TypeError):
            raise EmbedError("bad_response", 200) from None
        valid = (
            isinstance(vectors, list) and isinstance(truncated, list) and isinstance(counts, list)
            and isinstance(model, str) and isinstance(revision, str)
            and len(vectors) == len(truncated) == len(counts) == count
            and dim == EXPECTED_DIM
            and all(isinstance(v, list) and len(v) == EXPECTED_DIM for v in vectors)
            and all(isinstance(n, int) and not isinstance(n, bool) for n in counts)
        )
        if not valid:
            raise EmbedError("bad_response", 200)
        if self._revision is None:
            self._revision = revision
        elif revision != self._revision:
            raise EmbedRevisionChanged("revision_changed", 200)
        return data
