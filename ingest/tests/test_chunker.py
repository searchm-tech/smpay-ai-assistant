import re

from notion_ingest.chunker import MAX_CHARS, MAX_PATH_CHARS, MIN_CHARS, TARGET_CHARS, Chunk, chunk_document

TITLE = "충전 한도 안내"


def sentence(i: int) -> str:
    return f"{i}번째 안내 문장은 광고비 충전 한도와 승인 절차에 대해 설명합니다."


def paragraph(n: int, start: int = 0) -> str:
    return " ".join(sentence(start + i) for i in range(n))


def words(text: str) -> set[str]:
    return set(re.findall(r"\S+", text))


def joined(chunks: list[Chunk]) -> str:
    return "\n".join(c.content for c in chunks)


# ---------- 기본 ----------

def test_empty_or_heading_only_text_yields_no_chunks():
    assert chunk_document(TITLE, "") == []
    assert chunk_document(TITLE, "   \n\n") == []
    assert chunk_document(TITLE, "# 제목만\n\n## 소제목만") == []


def test_short_text_is_a_single_chunk_with_the_title_as_path():
    chunks = chunk_document(TITLE, "한도는 관리자 승인 후 변경됩니다.")
    assert len(chunks) == 1
    assert chunks[0].index == 0 and chunks[0].heading_path == TITLE
    assert chunks[0].content == "한도는 관리자 승인 후 변경됩니다."


def test_indices_are_contiguous_from_zero():
    text = "\n\n".join(f"## 섹션 {i}\n{paragraph(8, i * 10)}" for i in range(6))
    chunks = chunk_document(TITLE, text)
    assert [c.index for c in chunks] == list(range(len(chunks))) and len(chunks) > 1


# ---------- 헤딩 경로 ----------

def test_heading_path_nests_under_the_title():
    text = (
        "## 신청\n" + paragraph(6) + "\n\n### 승인\n" + paragraph(6, 100) + "\n\n## 반려\n" + paragraph(6, 200)
    )
    paths = [c.heading_path for c in chunk_document(TITLE, text)]
    assert paths[0] == f"{TITLE} > 신청"
    assert f"{TITLE} > 신청 > 승인" in paths
    assert paths[-1] == f"{TITLE} > 반려"  # 같은 레벨의 다음 헤딩은 "승인"이 아니라 "신청"을 대체한다


def test_first_heading_equal_to_the_title_is_not_repeated_in_the_path():
    chunks = chunk_document(TITLE, f"# {TITLE}\n\n본문입니다.\n\n## 신청\n절차입니다.")
    assert chunks[0].heading_path == TITLE
    assert not any(f"{TITLE} > {TITLE}" in c.heading_path for c in chunks)


def test_a_sibling_heading_replaces_the_previous_one_in_the_path():
    text = "## A\n" + paragraph(8) + "\n\n## B\n" + paragraph(8, 100)
    paths = {c.heading_path for c in chunk_document(TITLE, text)}
    assert paths == {f"{TITLE} > A", f"{TITLE} > B"}


def test_heading_like_lines_inside_code_fences_are_content_not_headings():
    text = "## 설정\n```\n# 주석입니다\n## 이것도 주석\nkey=value\n```"
    chunks = chunk_document(TITLE, text)
    assert all("주석" not in c.heading_path for c in chunks)
    assert "# 주석입니다" in joined(chunks)


# ---------- 크기와 무손실 ----------

def test_no_chunk_exceeds_the_hard_limit_for_hostile_inputs():
    inputs = [
        "가" * 5000,  # 공백 없는 한 덩어리
        "문장입니다. " * 800,
        "\n".join(f"- 항목 {i} " + "가" * 40 for i in range(200)),
        "```\n" + "\n".join(f"line {i}" for i in range(500)) + "\n```",
        "x" * 30 + "\n\n" + "y" * 1500,
    ]
    for text in inputs:
        for c in chunk_document(TITLE, text):
            assert 0 < len(c.content) <= MAX_CHARS, (len(c.content), text[:20])


def test_every_word_of_the_input_survives_in_some_chunk():
    text = (
        f"# {TITLE}\n\n" + paragraph(30) + "\n\n## 표\n" + "\n".join(f"행{i} | 값{i} | 설명{i}" for i in range(60))
        + "\n\n## 목록\n" + "\n".join(f"- 항목{i} 설명입니다" for i in range(80))
        + "\n\n```\nprint('hello')\nvalue = 1\n```"
    )
    original = words(re.sub(r"^#+ .*$", "", text, flags=re.M))
    assert original <= words(joined(chunk_document(TITLE, text)))


def test_chunks_aim_for_the_target_size_not_tiny_pieces():
    chunks = chunk_document(TITLE, paragraph(60))
    body = chunks[:-1]  # 마지막 조각은 짧을 수 있다
    assert len(chunks) > 3 and all(len(c.content) >= TARGET_CHARS * 0.5 for c in body)


# ---------- 표 · 코드 · 겹침 ----------

def test_a_long_table_splits_on_row_boundaries_and_repeats_the_header_row():
    rows = ["항목 | 한도 | 비고"] + [f"상품{i} | {i}00만원 | 설명{i}" for i in range(80)]
    chunks = chunk_document(TITLE, "\n".join(rows))
    assert len(chunks) > 1
    for c in chunks:
        assert c.content.splitlines()[0] == rows[0]
    body_rows = [line for c in chunks for line in c.content.splitlines() if line != rows[0]]
    assert set(rows[1:]) <= set(body_rows)
    assert all(line in rows for line in body_rows)  # 행이 중간에서 잘리지 않았다


def test_a_code_block_that_fits_stays_whole_inside_one_chunk():
    code = "```\n" + "\n".join(f"step{i} = run()" for i in range(12)) + "\n```"
    text = paragraph(6) + "\n\n" + code + "\n\n" + paragraph(6, 50)
    assert any(code in c.content for c in chunk_document(TITLE, text))


def test_a_split_inside_a_section_overlaps_by_the_previous_sentence():
    chunks = chunk_document(TITLE, paragraph(40))
    assert len(chunks) > 2
    sentence_re = re.compile(r"\d+번째 안내 문장은 광고비 충전 한도와 승인 절차에 대해 설명합니다\.")
    for prev, nxt in zip(chunks, chunks[1:]):
        last = sentence_re.findall(prev.content)[-1]
        assert nxt.content.startswith(last)  # 다음 조각은 직전 조각의 마지막 문장으로 시작한다
        assert len(sentence_re.findall(nxt.content)) >= 2  # 겹침 문장만 있는 조각은 아니다


def test_no_overlap_across_a_heading_boundary():
    text = "## A\n" + paragraph(6) + "\n\n## B\n" + paragraph(6, 100)
    chunks = chunk_document(TITLE, text)
    a = [c for c in chunks if c.heading_path.endswith("> A")]
    b = [c for c in chunks if c.heading_path.endswith("> B")]
    assert a and b
    assert not any(re.search(r"(?<!\d)5번째", c.content) for c in b)  # A의 마지막 문장이 B로 새지 않는다
    assert not any(re.search(r"(?<!\d)100번째", c.content) for c in a)


# ---------- 작은 섹션 병합 ----------

def test_tiny_sections_are_merged_into_a_neighbour_and_keep_their_heading_text():
    text = "## 개요\n" + paragraph(8) + "\n\n## 비고\n참고하세요.\n\n## 문의\n담당자에게 연락합니다."
    chunks = chunk_document(TITLE, text)
    assert all(len(c.content) >= MIN_CHARS for c in chunks)
    merged = joined(chunks)
    assert "참고하세요." in merged and "담당자에게 연락합니다." in merged
    assert "비고" in merged and "문의" in merged  # 헤딩 문구가 본문에 남아 검색에서 사라지지 않는다


def test_a_lone_tiny_document_is_kept_even_though_it_is_below_the_minimum():
    chunks = chunk_document(TITLE, "짧음")
    assert [c.content for c in chunks] == ["짧음"]


# ---------- 결정성과 임베딩 입력 ----------

def test_same_input_always_gives_the_same_chunks():
    text = "## A\n" + paragraph(30) + "\n\n## B\n" + paragraph(30, 40)
    assert chunk_document(TITLE, text) == chunk_document(TITLE, text)


def test_embed_text_prepends_the_heading_path_and_stays_within_the_embedder_limit():
    chunk = chunk_document(TITLE, "## 신청\n신청서를 작성합니다.")[0]
    assert chunk.embed_text == f"{chunk.heading_path}\n{chunk.content}"
    deep = "\n".join(f"{'#' * min(i + 1, 4)} " + "아주긴제목" * 40 for i in range(4)) + "\n\n본문입니다."
    for c in chunk_document("긴" * 600, deep + "\n\n" + paragraph(20)):
        assert len(c.embed_text) <= 2000


# ---------- 변형 테스트로 보강한 케이스 ----------

def test_unbreakable_long_text_is_split_without_losing_any_character():
    for text in ("가" * 5000, "x" * 30 + "\n\n" + "y" * 1500):
        body = "".join(c.content for c in chunk_document(TITLE, text))
        for ch in set(text) - {"\n"}:
            assert body.count(ch) >= text.count(ch), ch


def test_a_code_block_with_blank_lines_is_not_cut_at_the_blank_line():
    code = "```\n" + "\n".join(f"first{i} = compute({i})" for i in range(16)) + "\n\n" + "\n".join(
        f"second{i} = compute({i})" for i in range(16)
    ) + "\n```"
    assert 600 < len(code) < MAX_CHARS
    assert any(code in c.content for c in chunk_document(TITLE, paragraph(5) + "\n\n" + code))


def test_the_heading_path_is_capped():
    deep = "\n".join(f"{'#' * (i + 1)} " + f"제목{i}" * 80 for i in range(4)) + "\n\n" + paragraph(6)
    chunks = chunk_document("긴" * 600, deep)
    assert chunks and all(len(c.heading_path) <= MAX_PATH_CHARS for c in chunks)


# ---------- 임베딩 서버가 truncated 라고 한 청크를 반으로 나눌 때 ----------

def test_split_in_half_prefers_paragraph_then_line_then_sentence_then_space():
    from notion_ingest.chunker import split_in_half

    para = split_in_half("가나다라마바사\n\n아자차카타파하")
    assert para == ["가나다라마바사", "아자차카타파하"]
    line = split_in_half("가나다라마바사\n아자차카타파하")
    assert line == ["가나다라마바사", "아자차카타파하"]
    sent = split_in_half("첫 문장입니다. 둘째 문장입니다.")
    assert sent == ["첫 문장입니다.", "둘째 문장입니다."]
    space = split_in_half("alpha beta gamma delta")
    assert len(space) == 2 and " ".join(space) == "alpha beta gamma delta"


def test_split_in_half_cuts_near_the_middle_and_never_loses_text():
    from notion_ingest.chunker import split_in_half

    for text in (paragraph(12), "가" * 1001, "x " * 300, "한\n" * 101):
        left, right = split_in_half(text)
        assert left and right
        assert abs(len(left) - len(right)) <= len(text) * 0.35
        assert "".join(left.split()) + "".join(right.split()) == "".join(text.split())


def test_text_that_cannot_be_split_is_returned_whole():
    from notion_ingest.chunker import split_in_half

    assert split_in_half("가") == ["가"] and split_in_half("   ") == []
