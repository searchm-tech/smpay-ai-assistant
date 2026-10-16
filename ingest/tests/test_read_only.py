"""Notion은 읽기만 한다. 쓰기 호출(생성·수정·삭제)이 코드에 생기면 이 테스트가 실패해야 한다.

실제 보증은 Notion 통합의 권한('Read content'만 체크)이고, 여기서는 코드 쪽 방어를 고정한다.
"""

import re
from pathlib import Path

import httpx

from notion_ingest.crawler import crawl
from notion_ingest.notion_client import NotionClient
from tests.conftest import child_tag
from tests.fixtures import page_url, pid

SRC = Path(__file__).resolve().parents[1] / "notion_ingest"
ALLOWED_PATH = re.compile(r"^/v1/pages/[0-9a-f-]{36}(/markdown)?$")


class RecordingNotion:
    """GET /v1/pages/{id} 와 /markdown 만 응답하고 모든 요청을 기록하는 HTTP 모의 서버."""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.pages = {
            pid(1): ("루트", "# 루트\n" + child_tag(2, "A"), None),
            pid(2): ("A", "# A\n본문", pid(1)),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        match = re.match(r"^/v1/pages/([0-9a-f-]{36})(/markdown)?$", request.url.path)
        if request.method != "GET" or not match or match.group(1) not in self.pages:
            return httpx.Response(405, json={"message": "unexpected"})
        title, markdown, parent = self.pages[match.group(1)]
        if match.group(2):
            return httpx.Response(200, json={"markdown": markdown, "truncated": False, "unknown_block_ids": []})
        number = int(match.group(1).replace("-", ""), 16)
        return httpx.Response(200, json={
            "id": match.group(1), "url": page_url(number, title), "last_edited_time": "2026-10-01T00:00:00.000Z",
            "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
            "parent": {"type": "page_id", "page_id": parent} if parent else {"type": "workspace", "workspace": True},
            "in_trash": False,
        })

    def client(self) -> NotionClient:
        return NotionClient(
            "ntn_test", http=httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=lambda _s: None
        )


def test_crawl_sends_only_get_requests_to_the_two_read_endpoints():
    server = RecordingNotion()
    result = crawl(server.client(), [pid(1)])
    assert [d.title for d in result.documents] == ["루트", "A"]
    assert server.requests and {m for m, _ in server.requests} == {"GET"}
    assert all(ALLOWED_PATH.match(p) for _, p in server.requests)


def test_notion_client_source_has_no_write_calls():
    source = (SRC / "notion_client.py").read_text(encoding="utf-8")
    code = re.sub(r"#.*", "", source)
    assert not re.search(r"\.(post|put|patch|delete|request|send|stream)\(", code)
    assert not re.search(r"\bmethod\s*=", code)
    paths = set(re.findall(r'f?"(/v1/[^"]*)"', code))
    assert paths == {"/v1/pages/{page_id}", "/v1/pages/{page_id}/markdown"}, paths


def test_only_the_notion_client_knows_the_notion_host_and_token():
    for path in SRC.glob("*.py"):
        if path.name == "notion_client.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "api.notion.com" not in text, path.name
        assert "NOTION_TOKEN" not in text or path.name in {"config.py", "cli.py"}, path.name
