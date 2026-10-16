"""민감정보로 보이는 패턴을 센다. **개수만 반환**하고 매칭된 값은 어디에도 남기지 않는다."""

from __future__ import annotations

import re

from notion_ingest.cleaner import SIGNED_URL_MARKERS, URL_RE

PATTERNS: dict[str, re.Pattern] = {
    "biz_reg_no": re.compile(r"\b\d{3}-\d{2}-\d{5}\b"),
    "rrn": re.compile(r"\b\d{6}-[1-4]\d{6}\b"),
    "card": re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone_kr": re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b"),
    "api_key_like": re.compile(
        r"\b(?:sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}"
        r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|secret_[A-Za-z0-9]{30,}|ntn_[A-Za-z0-9]{30,})"
    ),
    # 값이 대입되는 모양("비밀번호: ...", "password = ...")만 센다. 단어만 나오는 일반 문장은 제외해 잡음을 줄인다.
    "secret_keyword": re.compile(r"(비밀번호|패스워드|password|passwd|pwd|secret|api[_ -]?key|토큰|token)\s*[:=：]", re.I),
}


def scan_sensitive(text: str) -> dict[str, int]:
    hits = {name: len(pattern.findall(text)) for name, pattern in PATTERNS.items()}
    signed = sum(1 for m in URL_RE.finditer(text) if SIGNED_URL_MARKERS.search(m.group(0)))
    if signed:
        hits["signed_url"] = signed
    return {name: count for name, count in hits.items() if count}
