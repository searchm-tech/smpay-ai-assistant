from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_DASHED = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# URL 경로 조각 끝의 ID. 슬러그 뒤에서만 인정한다("제목-<id>") — 16진수 문자가 앞에 붙어 길이가 어긋나는 경우를 거른다.
_SEGMENT_HEX = re.compile(r"(?:^|-)([0-9a-fA-F]{32})$")
_SEGMENT_DASHED = re.compile(r"(?:^|-)([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def _dash(hex32: str) -> str:
    h = hex32.lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def normalize_page_id(value: str | None) -> str | None:
    """페이지 ID(하이픈 유무 무관) 또는 Notion URL에서 ID를 뽑아 하이픈 있는 소문자 UUID로 만든다. 못 찾으면 None."""
    v = (value or "").strip()
    if not v:
        return None
    if _DASHED.fullmatch(v):
        return v.lower()
    if _HEX32.fullmatch(v):
        return _dash(v)
    if "://" not in v and "notion.so" not in v:
        return None

    url = urlparse(v if "://" in v else f"https://{v}")
    for p in parse_qs(url.query).get("p", []):
        found = normalize_page_id(p)
        if found:
            return found
    for segment in reversed(url.path.split("/")):
        m = _SEGMENT_DASHED.search(segment)
        if m:
            return m.group(1).lower()
        m = _SEGMENT_HEX.search(segment)
        if m:
            return _dash(m.group(1))
    return None
