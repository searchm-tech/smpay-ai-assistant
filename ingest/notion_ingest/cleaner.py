"""Notion enhanced markdown(v1)을 임베딩용 텍스트로 정제한다.

형식은 공식 문서 기반이며 일부(멘션 등)는 추정이다. 실제 응답과 다르면 tests/fixtures.py 와 함께 고친다(R11).
정제 결과는 멱등이어야 하고, 서명된 파일 URL은 어떤 입력에서도 남아서는 안 된다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from notion_ingest.ids import normalize_page_id

# 서명된(만료되는) 파일 URL. DB·LLM 컨텍스트에 남으면 접근 토큰이 새고, 매번 값이 달라 매일 "변경"으로 오인된다.
SIGNED_URL_MARKERS = re.compile(r"X-Amz-|prod-files-secure|secure\.notion-static|file\.notion\.so", re.I)
URL_RE = re.compile(r"https?://[^\s)\]>\"'`]+")
REMOVED_LINK = "(링크 제거됨)"

KNOWN_TAGS = {
    "unknown", "table_of_contents", "custom-block", "file", "video", "audio", "pdf", "table", "tr", "td", "th",
    "colgroup", "col", "page", "database", "br", "callout", "details", "summary", "columns", "column",
    "synced_block", "synced_block_reference", "meeting-notes",
}
MEDIA_LABELS = {"file": "첨부", "video": "동영상", "audio": "오디오", "pdf": "PDF"}
WRAPPERS = "callout|details|summary|columns|column|synced_block_reference|synced_block|meeting-notes"

_FENCED = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]+`")
_UNKNOWN = re.compile(r"<unknown\b([^>]*)>(?:\s*</unknown>)?", re.I)
_ALT = re.compile(r'\balt\s*=\s*"([^"]*)"')
_NOISE = re.compile(r"<(?:table_of_contents|custom-block)\b[^>]*>", re.I)
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_MEDIA = re.compile(r"<(file|video|audio|pdf)\b[^>]*?(?:/>|>(.*?)</\1>)", re.S | re.I)
_TABLE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.S | re.I)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_PAGE = re.compile(r"<page\b([^>]*?)(?:/>|>(.*?)</page>)", re.S | re.I)
_DATABASE = re.compile(r"<database\b([^>]*?)(?:/>|>(.*?)</database>)", re.S | re.I)
_URL_ATTR = re.compile(r'\burl\s*=\s*"([^"]*)"')
_MENTION = re.compile(r"<(mention-[a-z-]+)\b([^>]*?)(?:/>|>(.*?)</\1>)", re.S | re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_WRAPPER_CLOSE = re.compile(rf"</(?:{WRAPPERS})>", re.I)
_WRAPPER_OPEN = re.compile(rf"<(?:{WRAPPERS})\b[^>]*>", re.I)
_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)]*)\)")
_ANY_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9_-]*)\b[^<>]*>")
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")


@dataclass(frozen=True)
class ChildRef:
    page_id: str
    title: str


@dataclass
class CleanResult:
    text: str
    children: list[ChildRef] = field(default_factory=list)
    databases: list[ChildRef] = field(default_factory=list)
    unknown_count: int = 0
    unknown_by_type: dict[str, int] = field(default_factory=dict)
    unrecognized_tags: dict[str, int] = field(default_factory=dict)
    table_count: int = 0


def content_hash(text: str) -> str:
    """정제된 텍스트의 해시. 줄바꿈(CRLF/LF) 차이는 무시한다."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def strip_signed_urls(text: str) -> str:
    return URL_RE.sub(lambda m: REMOVED_LINK if SIGNED_URL_MARKERS.search(m.group(0)) else m.group(0), text)


def _attr(attrs: str, name: str) -> str:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', attrs)
    return m.group(1) if m else ""


def _plain(inner: str | None) -> str:
    return re.sub(r"<[^>]+>", "", inner or "").strip()


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "  ")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_markdown(markdown: str) -> CleanResult:
    result = CleanResult(text="")

    # 코드(블록·인라인)는 변환하지 않고 그대로 둔다. 안의 태그를 문서 구조로 오인하지 않기 위해 자리표시자로 바꿔둔다.
    protected: list[str] = []

    def protect(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    text = markdown.replace("\x00", "")
    text = _INLINE.sub(protect, _FENCED.sub(protect, text))

    def unknown(m: re.Match) -> str:
        alt = _ALT.search(m.group(1))
        kind = (alt.group(1) if alt else "") or "unknown"
        result.unknown_count += 1
        result.unknown_by_type[kind] = result.unknown_by_type.get(kind, 0) + 1
        return ""

    text = _NOISE.sub("", _UNKNOWN.sub(unknown, text))

    # 이미지·파일: 캡션/이름만 남기고 URL(서명 포함)은 버린다
    text = _IMAGE.sub(lambda m: f"(이미지: {m.group(1).strip()})" if m.group(1).strip() else "(이미지)", text)
    text = _MEDIA.sub(
        lambda m: f"({MEDIA_LABELS[m.group(1).lower()]}: {_plain(m.group(2))})" if _plain(m.group(2))
        else f"({MEDIA_LABELS[m.group(1).lower()]})",
        text,
    )

    def table(m: re.Match) -> str:
        result.table_count += 1
        rows = []
        for row in _ROW.findall(m.group(1)):
            cells = [re.sub(r"\s+", " ", _BR.sub(" ", c)).strip() for c in _CELL.findall(row)]
            if any(cells):
                rows.append(" | ".join(cells))
        return "\n".join(rows)

    text = _TABLE.sub(table, text)

    def reference(label: str, sink: list[ChildRef]):
        def replace(m: re.Match) -> str:
            title = _plain(m.group(2))
            url = _URL_ATTR.search(m.group(1))
            page_id = normalize_page_id(url.group(1)) if url else None
            if page_id:
                sink.append(ChildRef(page_id, title))
            return f"({label}: {title})" if title else f"({label})"

        return replace

    text = _PAGE.sub(reference("하위 페이지", result.children), text)
    text = _DATABASE.sub(reference("데이터베이스", result.databases), text)

    def mention(m: re.Match) -> str:
        name, attrs, inner = m.group(1).lower(), m.group(2), (m.group(3) or "").strip()
        if inner:
            return inner
        if name == "mention-date":
            start, end = _attr(attrs, "start"), _attr(attrs, "end")
            return f"{start} ~ {end}" if start and end else start
        return ""

    text = _MENTION.sub(mention, text)
    text = _BR.sub("\n", text)
    text = _WRAPPER_OPEN.sub("", _WRAPPER_CLOSE.sub("\n", text))
    text = _LINK.sub(lambda m: m.group(1), text)

    # 알 수 없는 태그는 지우지 않고 남기되 개수를 센다 → 형식이 바뀌면 리포트에서 드러난다
    for m in _ANY_TAG.finditer(text):
        name = m.group(1).lower()
        if name not in KNOWN_TAGS and not name.startswith("mention-"):
            result.unrecognized_tags[name] = result.unrecognized_tags.get(name, 0) + 1

    text = _PLACEHOLDER.sub(lambda m: protected[int(m.group(1))], text)
    result.text = _normalize(strip_signed_urls(text))  # 안전망: 코드 안을 포함해 서명 URL이 남지 않게
    return result
