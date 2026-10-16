from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from notion_ingest.ids import normalize_page_id

logger = logging.getLogger("notion_ingest")

NOTION_VERSION = "2026-03-11"
BASE_URL = "https://api.notion.com"
RETRYABLE_STATUS = {500, 502, 503, 504, 529}


class NotionError(RuntimeError):
    """Notion 호출 실패. 메시지에는 종류와 상태코드만 담는다(응답 본문·토큰·요청 내용은 담지 않음)."""

    def __init__(self, kind: str, status: int | None = None):
        super().__init__(f"notion request failed: kind={kind} status={status}")
        self.kind = kind
        self.status = status


class NotionNotFound(NotionError):
    """페이지가 없거나 통합에 공유되지 않음(Notion은 둘을 구분하지 않는다)."""


class NotionAuthError(NotionError):
    """토큰이 잘못되었거나 권한(Read content)이 없음. 재시도해도 소용없으므로 전체 실행을 중단한다."""


@dataclass(frozen=True)
class PageMeta:
    id: str
    url: str
    title: str
    last_edited_time: str | None
    parent_type: str | None
    parent_id: str | None
    in_trash: bool


@dataclass(frozen=True)
class MarkdownResult:
    markdown: str
    truncated: bool
    unknown_block_ids: tuple[str, ...]


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        http: httpx.Client | None = None,
        rate_per_sec: float = 2.5,
        max_retries: int = 5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = BASE_URL,
        version: str = NOTION_VERSION,
        timeout: float = 30.0,
    ) -> None:
        self._headers = {"Authorization": f"Bearer {token}", "Notion-Version": version}
        self._http = http or httpx.Client(timeout=timeout)
        self._base_url = base_url
        self._interval = 1.0 / rate_per_sec
        self._max_retries = max_retries
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0

    def __repr__(self) -> str:  # 토큰이 든 헤더가 repr에 나오지 않게
        return "NotionClient()"

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ 공개 API

    def get_page(self, page_id: str) -> PageMeta:
        data = self._request(f"/v1/pages/{page_id}")
        if not isinstance(data, dict) or "id" not in data:
            raise NotionError("bad_response", 200)

        parent = data.get("parent") if isinstance(data.get("parent"), dict) else {}
        parent_type = parent.get("type") if isinstance(parent.get("type"), str) else None
        raw_parent_id = parent.get(parent_type) if parent_type else None
        title = ""
        properties = data.get("properties") if isinstance(data.get("properties"), dict) else {}
        for prop in properties.values():  # 제목 속성의 이름은 DB 행마다 다를 수 있어 이름이 아니라 type으로 찾는다
            if isinstance(prop, dict) and prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []) if isinstance(t, dict))
                break
        return PageMeta(
            id=normalize_page_id(str(data["id"])) or str(data["id"]),
            url=str(data.get("url", "")),
            title=title,
            last_edited_time=data.get("last_edited_time") if isinstance(data.get("last_edited_time"), str) else None,
            parent_type=parent_type,
            parent_id=normalize_page_id(raw_parent_id) if isinstance(raw_parent_id, str) else None,
            in_trash=bool(data.get("in_trash") or data.get("archived")),
        )

    def get_markdown(self, page_id: str) -> MarkdownResult:
        data = self._request(f"/v1/pages/{page_id}/markdown")
        if not isinstance(data, dict) or not isinstance(data.get("markdown"), str):
            raise NotionError("bad_response", 200)
        ids = tuple(
            i for i in (normalize_page_id(str(x)) for x in data.get("unknown_block_ids") or []) if i
        )
        return MarkdownResult(markdown=data["markdown"], truncated=bool(data.get("truncated")), unknown_block_ids=ids)

    # ------------------------------------------------------------ 내부

    def _throttle(self) -> None:
        now = self._clock()
        wait = self._next_at - now
        if wait > 0.001:
            self._sleep(wait)
            now = self._clock()
        self._next_at = max(now, self._next_at) + self._interval

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(30.0, float(2**attempt))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        try:
            return min(60.0, max(0.0, float(response.headers["Retry-After"])))
        except (KeyError, ValueError):
            return None

    def _request(self, path: str):
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._http.get(self._base_url + path, headers=self._headers)
            except httpx.TransportError:  # 원래 메시지에는 URL 등이 섞일 수 있어 버린다
                kind, status, wait = "network", None, self._backoff(attempt)
            else:
                status = response.status_code
                if status == 200:
                    try:
                        return response.json()
                    except ValueError:
                        raise NotionError("bad_response", 200) from None
                if status == 404:
                    raise NotionNotFound("not_found", 404)
                if status in (401, 403):
                    raise NotionAuthError("unauthorized" if status == 401 else "forbidden", status)
                if status == 429:
                    header_wait = self._retry_after(response)
                    kind = "rate_limited"
                    wait = max(1.0, header_wait if header_wait is not None else self._backoff(attempt))
                elif status in RETRYABLE_STATUS:
                    kind, wait = f"http_{status}", self._backoff(attempt)
                elif 400 <= status < 500:
                    raise NotionError("bad_request", status)
                else:
                    raise NotionError(f"http_{status}", status)

            if attempt >= self._max_retries:
                raise NotionError(kind, status)
            logger.warning("notion retry kind=%s attempt=%d wait=%.1fs", kind, attempt + 1, wait)
            self._sleep(wait)
            attempt += 1
