"""실제 NotionClient → 크롤러 → 정제 → 리포트 → CLI 전체 경로를 가짜 Notion HTTP 서버로 검증한다."""

import json
import re

import httpx

from notion_ingest.cli import main
from notion_ingest.notion_client import NotionClient
from tests.conftest import child_tag
from tests.fixtures import SENTINEL, SIGNED, TOKEN, page_url, pid


def notion_server(pages: dict[int, dict], *, rate_limit_first_markdown_of: int | None = None):
    """pages: n -> {title, md, parent(n|None)}. 요청 기록과 헤더를 남긴다."""
    seen = {"requests": [], "auth": set(), "versions": set(), "limited": False}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["requests"].append(request.url.path)
        seen["auth"].add(request.headers.get("authorization"))
        seen["versions"].add(request.headers.get("notion-version"))
        m = re.fullmatch(r"/v1/pages/([0-9a-f-]{36})(/markdown)?", request.url.path)
        if not m:
            return httpx.Response(404, json={"code": "invalid_request_url"})
        n = int(m.group(1).replace("-", ""), 16)
        page = pages.get(n)
        if page is None:
            return httpx.Response(404, json={"code": "object_not_found"})
        if m.group(2):  # /markdown
            if n == rate_limit_first_markdown_of and not seen["limited"]:
                seen["limited"] = True
                return httpx.Response(429, headers={"Retry-After": "1"}, json={"code": "rate_limited"})
            return httpx.Response(200, json={"object": "page_markdown", "id": m.group(1), "markdown": page["md"],
                                             "truncated": False, "unknown_block_ids": []})
        parent = {"type": "workspace", "workspace": True} if page.get("parent") is None else {"type": "page_id", "page_id": pid(page["parent"])}
        return httpx.Response(200, json={
            "object": "page", "id": m.group(1), "url": page_url(n, page["title"]), "last_edited_time": "2026-10-01T00:00:00.000Z",
            "in_trash": False, "parent": parent,
            "properties": {"title": {"type": "title", "title": [{"plain_text": page["title"]}]}},
        })

    return handler, seen


def run(handler, argv, roots=None):
    out, err = [], []
    environ = {"NOTION_TOKEN": TOKEN, "NOTION_ROOT_PAGE_IDS": roots or pid(1)}
    sleeps: list[float] = []

    def factory(settings):
        return NotionClient(settings.token, http=httpx.Client(transport=httpx.MockTransport(handler)),
                            sleep=sleeps.append, rate_per_sec=1000)

    code = main(argv, environ=environ, client_factory=factory, out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err), sleeps


def test_full_pipeline_over_http_with_a_rate_limit_hiccup_and_signed_urls():
    pages = {
        1: {"title": "루트", "parent": None, "md": f"# 루트\n{SENTINEL}\n![표](" + SIGNED + ")\n" + child_tag(2, "정책") + "\n" + child_tag(3, "FAQ")},
        2: {"title": "정책", "parent": 1, "md": "# 정책\n문의 02-123-4567\n" + child_tag(4, "세부")},
        3: {"title": "FAQ", "parent": 1, "md": "# FAQ\n<table><tr><td>질문</td><td>답</td></tr></table>"},
        4: {"title": "세부", "parent": 2, "md": "# 세부"},
        9: {"title": "다른 곳(허용 목록 밖)", "parent": None, "md": "# 비공개"},
    }
    handler, seen = notion_server(pages, rate_limit_first_markdown_of=2)
    code, out, err, sleeps = run(handler, ["measure", "--json"])

    report = json.loads(out)
    assert code == 0 and report["documents"] == 4 and report["complete"] is True
    assert report["tables"] == 1 and report["depth"]["max"] == 2
    assert report["sensitive"]["totals"].get("phone_kr") == 1 and report["sensitive"]["totals"].get("signed_url") == 1
    assert 1 in sleeps  # 429의 Retry-After를 따랐다
    # 허용 목록 밖 페이지(9)에는 요청 자체가 없었다
    assert not any(pid(9) in p for p in seen["requests"])
    # 모든 요청이 올바른 인증 헤더·API 버전으로 나갔다
    assert seen["auth"] == {f"Bearer {TOKEN}"} and seen["versions"] == {"2026-03-11"}
    # 출력(리포트)에 본문·토큰·서명 URL이 없다
    for forbidden in (SENTINEL, TOKEN, "X-Amz-", "prod-files-secure"):
        assert forbidden not in out + err


def test_wrong_token_surfaces_as_a_generic_auth_error_over_http():
    handler = lambda request: httpx.Response(401, json={"code": "unauthorized", "message": f"{TOKEN} {SENTINEL}"})  # noqa: E731
    code, out, err, _ = run(handler, ["measure"])
    assert code == 1 and "401" in err
    assert TOKEN not in out + err and SENTINEL not in out + err


def test_unshared_root_is_reported_and_blocks_deletion_semantics():
    handler, _ = notion_server({})  # 어떤 페이지도 통합에 공유되지 않음
    code, out, _, _ = run(handler, ["measure", "--json"])
    report = json.loads(out)
    assert code == 0 and report["documents"] == 0 and report["complete"] is False
    assert "root_not_found" in report["incomplete_reasons"] and report["errors"] == {"root_not_found": 1}
