import logging

import httpx
import pytest

from notion_ingest.notion_client import NotionAuthError, NotionClient, NotionError, NotionNotFound
from tests.fixtures import SENTINEL, TOKEN, page_url, pid


def page_json(n=1, title="제목", parent=None, **extra):
    body = {
        "object": "page", "id": pid(n), "url": page_url(n), "last_edited_time": "2026-10-01T00:00:00.000Z",
        "in_trash": False, "parent": parent or {"type": "workspace", "workspace": True},
        "properties": {"title": {"id": "title", "type": "title", "title": [{"plain_text": title}]}},
    }
    body.update(extra)
    return body


def make(handler, **kw):
    sleeps: list[float] = []
    state = {"t": 0.0}

    def sleep(s):
        sleeps.append(s)
        state["t"] += s

    client = NotionClient(
        TOKEN, http=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: state["t"], sleep=sleep, **{"rate_per_sec": 1000, **kw},
    )
    return client, sleeps, state


def retry_sleeps(sleeps):
    return [s for s in sleeps if s >= 0.5]  # 속도 제한용 짧은 대기는 제외


def test_request_has_auth_and_version_headers_and_correct_path():
    seen = {}

    def handler(request):
        seen.update(auth=request.headers["authorization"], ver=request.headers["notion-version"],
                    method=request.method, path=request.url.path)
        return httpx.Response(200, json=page_json())

    make(handler)[0].get_page(pid(1))
    assert seen == {"auth": f"Bearer {TOKEN}", "ver": "2026-03-11", "method": "GET", "path": f"/v1/pages/{pid(1)}"}


def test_get_page_parses_meta_and_parent():
    parent = {"type": "page_id", "page_id": pid(7).replace("-", "")}
    meta = make(lambda r: httpx.Response(200, json=page_json(3, "하위", parent)))[0].get_page(pid(3))
    assert (meta.id, meta.title, meta.parent_type, meta.parent_id, meta.in_trash) == (pid(3), "하위", "page_id", pid(7), False)
    assert meta.last_edited_time == "2026-10-01T00:00:00.000Z" and meta.url == page_url(3)


def test_title_is_found_by_property_type_not_by_name():
    body = page_json()
    body["properties"] = {"이름": {"type": "title", "title": [{"plain_text": "행 "}, {"plain_text": "제목"}]}, "태그": {"type": "select"}}
    assert make(lambda r: httpx.Response(200, json=body))[0].get_page(pid(1)).title == "행 제목"


@pytest.mark.parametrize("extra", [{"in_trash": True}, {"archived": True}])
def test_trashed_or_archived_pages_are_flagged(extra):
    body = page_json()
    body["in_trash"] = False
    body.update(extra)
    assert make(lambda r: httpx.Response(200, json=body))[0].get_page(pid(1)).in_trash is True


def test_get_markdown_parses_fields_and_requests_the_markdown_path():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={"object": "page_markdown", "id": pid(1), "markdown": "# 본문",
                                         "truncated": True, "unknown_block_ids": [pid(5).replace("-", "")]})

    r = make(handler)[0].get_markdown(pid(1))
    assert seen["path"] == f"/v1/pages/{pid(1)}/markdown"
    assert (r.markdown, r.truncated, r.unknown_block_ids) == ("# 본문", True, (pid(5),))


def test_429_honors_retry_after_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "3"}, json={}) if len(calls) == 1 else httpx.Response(200, json=page_json())

    client, sleeps, _ = make(handler)
    client.get_page(pid(1))
    assert len(calls) == 2 and 3 in retry_sleeps(sleeps)


def test_429_without_retry_after_still_waits_before_retrying():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={}) if len(calls) == 1 else httpx.Response(200, json=page_json())

    client, sleeps, _ = make(handler)
    client.get_page(pid(1))
    assert retry_sleeps(sleeps) and retry_sleeps(sleeps)[0] >= 1


def test_5xx_uses_exponential_backoff():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={}) if len(calls) <= 2 else httpx.Response(200, json=page_json())

    client, sleeps, _ = make(handler)
    client.get_page(pid(1))
    assert retry_sleeps(sleeps) == [1, 2]


@pytest.mark.parametrize("status,kind", [(503, "http_503"), (529, "http_529"), (429, "rate_limited")])
def test_gives_up_after_max_retries_without_leaking_body_or_token(status, kind, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"message": f"{SENTINEL} {TOKEN}"})

    client, _, _ = make(handler, max_retries=2)
    with pytest.raises(NotionError) as e:
        client.get_page(pid(1))
    assert len(calls) == 3 and e.value.kind == kind and e.value.status == status
    for secret in (SENTINEL, TOKEN):
        assert secret not in str(e.value) and secret not in caplog.text


def test_404_is_not_found_without_retry():
    calls = []
    client, _, _ = make(lambda r: (calls.append(1), httpx.Response(404, json={"code": "object_not_found"}))[1])
    with pytest.raises(NotionNotFound):
        client.get_page(pid(1))
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_fatal_and_not_retried(status):
    calls = []
    client, _, _ = make(lambda r: (calls.append(1), httpx.Response(status, json={}))[1])
    with pytest.raises(NotionAuthError):
        client.get_page(pid(1))
    assert len(calls) == 1


def test_400_is_not_retried():
    calls = []
    client, _, _ = make(lambda r: (calls.append(1), httpx.Response(400, json={}))[1])
    with pytest.raises(NotionError) as e:
        client.get_page(pid(1))
    assert len(calls) == 1 and e.value.kind == "bad_request"


def test_network_errors_are_retried_and_do_not_leak_messages():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError(f"dns failed {SENTINEL} {TOKEN}")

    client, _, _ = make(handler, max_retries=2)
    with pytest.raises(NotionError) as e:
        client.get_page(pid(1))
    assert len(calls) == 3 and e.value.kind == "network"
    assert SENTINEL not in str(e.value) and TOKEN not in str(e.value)


@pytest.mark.parametrize("body", [{"unexpected": 1}, []])
def test_unexpected_response_shape_is_a_clean_error(body):
    with pytest.raises(NotionError) as e:
        make(lambda r: httpx.Response(200, json=body))[0].get_page(pid(1))
    assert e.value.kind == "bad_response"


def test_non_json_response_is_a_clean_error():
    with pytest.raises(NotionError) as e:
        make(lambda r: httpx.Response(200, content=b"<html>" + SENTINEL.encode()))[0].get_page(pid(1))
    assert e.value.kind == "bad_response" and SENTINEL not in str(e.value)


def test_rate_limiter_spaces_requests():
    client, sleeps, state = make(lambda r: httpx.Response(200, json=page_json()), rate_per_sec=2)
    for _ in range(3):
        client.get_page(pid(1))
    assert state["t"] == pytest.approx(1.0) and sleeps == [pytest.approx(0.5), pytest.approx(0.5)]


def test_repr_does_not_contain_the_token():
    client, _, _ = make(lambda r: httpx.Response(200, json=page_json()))
    assert TOKEN not in repr(client) and TOKEN not in str(client)
