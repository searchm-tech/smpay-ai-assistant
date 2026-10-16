"""정제된 마크다운을 헤딩 기준으로 검색용 청크로 나눈다 (순수 함수: 같은 입력은 항상 같은 결과).

크기 기본값(목표 500자 / 상한 900자 / 하한 80자)은 A5 측정 리포트의 실제 문서 길이로 확정한다.
청크 규칙을 바꾸면 CHUNKER_VERSION 을 올린다 → index_signature 가 바뀌어 모든 문서가 재색인된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHUNKER_VERSION = "1"
TARGET_CHARS = 500
MAX_CHARS = 900
MIN_CHARS = 80
MAX_PATH_CHARS = 300
OVERLAP_MAX_CHARS = 200  # 겹침으로 되풀이할 직전 문장(줄)의 최대 길이

_HEADING = re.compile(r"^(#{1,4})\s+(\S.*?)\s*$")
_FENCE = re.compile(r"^\s*```")
_SENTENCE_END = re.compile(r"(?<=[.!?。…])\s+")


@dataclass(frozen=True)
class Chunk:
    index: int
    heading_path: str  # "문서 제목 > 섹션 > 소제목"
    content: str  # 본문만(저장·표시용)

    @property
    def embed_text(self) -> str:
        """임베딩 입력: 짧은 섹션도 의미가 헤딩에 있으므로 경로를 앞에 붙인다. DB의 content에는 넣지 않는다."""
        return f"{self.heading_path}\n{self.content}"


@dataclass
class _Section:
    path: str
    heading_line: str | None
    blocks: list[str]


@dataclass
class _Item:
    path: str
    heading_line: str | None
    first: bool  # 섹션의 첫 조각인가(병합할 때 헤딩 문구를 본문에 남기기 위해)
    content: str


def _path(title: str, stack: list[tuple[int, str]]) -> str:
    parts: list[str] = []
    for part in [title.strip(), *(text for _, text in stack)]:
        if part and (not parts or parts[-1] != part):  # 첫 헤딩이 문서 제목과 같으면 반복하지 않는다
            parts.append(part)
    path = " > ".join(parts)
    return path if len(path) <= MAX_PATH_CHARS else "…" + path[-(MAX_PATH_CHARS - 1):]


def _blocks(lines: list[str]) -> list[str]:
    """빈 줄 기준으로 블록을 나눈다. 코드 펜스 안의 빈 줄은 블록을 끊지 않는다."""
    blocks: list[list[str]] = [[]]
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and not line.strip() and not _FENCE.match(line):
            if blocks[-1]:
                blocks.append([])
            continue
        blocks[-1].append(line)
    return [b for b in ("\n".join(block).strip("\n") for block in blocks) if b.strip()]


def _sections(title: str, text: str) -> list[_Section]:
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    path, heading_line = _path(title, stack), None
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        blocks = _blocks(lines)
        if blocks:
            sections.append(_Section(path, heading_line, blocks))

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            lines.append(line)
            continue
        flush()
        level, heading = len(match.group(1)), match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        path, heading_line, lines = _path(title, stack), line.strip(), []
    flush()
    return sections


def _hard_split(text: str, limit: int) -> list[str]:
    pieces: list[str] = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind(" ", limit // 2, limit)
        cut = cut if cut > 0 else limit
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def _atomize(unit: str) -> list[str]:
    """한 단위(문장·줄)가 상한을 넘으면 문장 → 공백 → 글자 수 순으로 더 쪼갠다."""
    if len(unit) <= MAX_CHARS:
        return [unit]
    out: list[str] = []
    for sentence in _SENTENCE_END.split(unit):
        out.extend([sentence] if len(sentence) <= MAX_CHARS else _hard_split(sentence, TARGET_CHARS))
    return out


def _pack_units(units: list[str], sep: str, limit: int, prefix: str = "") -> list[str]:
    """단위를 limit 근처까지 채워 조각을 만든다. 조각이 이어질 때 직전 단위 1개를 겹쳐 시작한다(prefix 가 있으면 겹침 대신 반복)."""
    flat = [part for unit in units for part in _atomize(unit)]
    pieces: list[list[str]] = []
    current: list[str] = []
    size = len(prefix)

    def render(parts: list[str]) -> str:
        return (prefix + sep.join(parts)) if prefix else sep.join(parts)

    for unit in flat:
        added = len(unit) + (len(sep) if current else 0)
        if current and size + added > limit:
            pieces.append(current)
            overlap = current[-1] if (not prefix and len(current) >= 2 and len(current[-1]) <= OVERLAP_MAX_CHARS) else None
            current = [overlap] if overlap is not None and len(overlap) + len(sep) + len(unit) <= limit else []
            size = len(prefix) + (len(current[0]) if current else 0)
            added = len(unit) + (len(sep) if current else 0)
        current.append(unit)
        size += added
    if current:
        pieces.append(current)
    return [render(parts) for parts in pieces]


def _split_block(block: str) -> list[str]:
    lines = block.split("\n")
    if len(lines) > 2 and all(" | " in line for line in lines) and len(lines[0]) <= MAX_CHARS // 3:
        header = lines[0]  # 표: 행 경계에서만 나누고 첫 행(헤더)을 각 조각 앞에 반복한다
        return _pack_units(lines[1:], "\n", TARGET_CHARS, prefix=header + "\n")
    if len(lines) > 1:
        return _pack_units(lines, "\n", TARGET_CHARS)
    return _pack_units(_SENTENCE_END.split(block), " ", TARGET_CHARS)


def _pack_section(blocks: list[str]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            chunks.append("\n\n".join(current))
        current, size = [], 0

    for block in blocks:
        if len(block) > MAX_CHARS:
            flush()
            chunks.extend(_split_block(block))
            continue
        added = len(block) + (2 if current else 0)
        if current and size + added > TARGET_CHARS:
            flush()
            added = len(block)
        current.append(block)
        size += added
    flush()
    return chunks


def _join(earlier: _Item, later: _Item) -> _Item:
    """두 조각을 합친다. 뒤 조각이 새 섹션의 첫 조각이면 그 헤딩 문구를 본문에 남긴다."""
    lead = (later.heading_line + "\n") if (later.first and later.heading_line) else ""
    return _Item(earlier.path, earlier.heading_line, earlier.first, f"{earlier.content}\n\n{lead}{later.content}")


def _merge_small(items: list[_Item]) -> list[_Item]:
    merged: list[_Item] = []
    i = 0
    while i < len(items):
        item = items[i]
        if len(item.content) < MIN_CHARS:
            if i + 1 < len(items) and len(_join(item, items[i + 1]).content) <= MAX_CHARS:
                items[i + 1] = _join(item, items[i + 1])
                i += 1
                continue
            if merged and len(_join(merged[-1], item).content) <= MAX_CHARS:
                merged[-1] = _join(merged[-1], item)
                i += 1
                continue
        merged.append(item)
        i += 1
    return merged


def chunk_document(title: str, text: str) -> list[Chunk]:
    items: list[_Item] = []
    for section in _sections(title, text):
        for position, content in enumerate(_pack_section(section.blocks)):
            content = content.strip()
            if content:
                items.append(_Item(section.path, section.heading_line, position == 0, content))
    return [Chunk(index, item.path, item.content) for index, item in enumerate(_merge_small(items))]


EMBED_INPUT_VERSION = "1"  # 임베딩 입력 방식(제목·경로 접두 등)을 바꾸면 올린다


def split_in_half(text: str) -> list[str]:
    """임베딩 서버가 입력을 잘랐다고(truncated) 알려 준 청크를 가운데 근처에서 둘로 나눈다.
    문단 → 줄 → 문장 → 공백 순으로 가운데에 가장 가까운 경계를 고르고, 없으면 글자 수 기준으로 자른다."""
    text = text.strip()
    if len(text) < 2:
        return [text] if text else []
    middle = len(text) / 2

    def nearest(positions: list[int]) -> int | None:
        usable = [p for p in positions if 0 < p < len(text)]
        return min(usable, key=lambda p: abs(p - middle)) if usable else None

    for pattern in (r"\n\s*\n", r"\n", r"(?<=[.!?。…])\s+", r"\s+"):
        cut = nearest([m.start() for m in re.finditer(pattern, text)])
        if cut is not None:
            left, right = text[:cut].strip(), text[cut:].strip()
            if left and right and min(len(left), len(right)) >= len(text) * 0.15:
                return [left, right]
    cut = len(text) // 2
    return [text[:cut].strip(), text[cut:].strip()]
