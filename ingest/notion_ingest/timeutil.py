"""수정 시각 비교용 정규화. DB(`+00:00`)와 Notion(`….000Z`)의 표기가 달라도 같은 순간이면 같게 본다."""

from __future__ import annotations

from datetime import datetime, timezone


def normalize_time(value: str | None) -> str | None:
    """UTC 밀리초 `YYYY-MM-DDTHH:MM:SS.mmmZ` 로 통일한다. 해석할 수 없는 값은 앞뒤 공백만 정리해 그대로 둔다."""
    if value is None:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # 시간대가 없으면 UTC 로 본다(Notion·Postgres 모두 UTC)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    """DB에 넘길 timestamptz 값. 해석할 수 없으면 None(수정 시각을 모르는 것으로 저장)."""
    normalized = normalize_time(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
