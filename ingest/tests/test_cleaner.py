import random
import re

import pytest

from notion_ingest.cleaner import ChildRef, clean_markdown, content_hash
from tests.fixtures import SIGNED, SIGNED_OTHER, page_url, sample_markdown

SIGNED_MARKERS = re.compile(r"X-Amz-|prod-files-secure|secure\.notion-static|file\.notion\.so", re.I)


def clean(md):
    return clean_markdown(md)


# ---------- 보존 / 변환 ----------

def test_headings_and_plain_text_are_preserved():
    t = clean(sample_markdown()).text
    assert "# 충전 한도 안내" in t and "## 신청 절차" in t
    assert "1. 신청서를 작성합니다." in t


def test_callout_details_columns_and_synced_blocks_are_flattened():
    t = clean(sample_markdown()).text
    assert "승인은 영업일 기준 1일 이내 처리됩니다." in t and "<callout" not in t
    assert "자주 묻는 질문" in t and "최대 1억 원입니다." in t and "<details" not in t and "<summary" not in t
    t2 = clean("<columns><column>왼쪽</column><column>오른쪽</column></columns>\n<synced_block>동기화 내용</synced_block>").text
    assert "왼쪽" in t2 and "오른쪽" in t2 and "동기화 내용" in t2 and "<column" not in t2 and "<synced" not in t2


def test_table_becomes_pipe_separated_rows():
    r = clean(sample_markdown())
    assert "구분 | 한도" in r.text and "일반 | 1,000만 원" in r.text
    assert "<table" not in r.text and "<tr" not in r.text and "<td" not in r.text
    assert r.table_count == 1


def test_br_becomes_newline():
    assert clean("첫줄<br>둘째줄<br/>셋째줄").text == "첫줄\n둘째줄\n셋째줄"


def test_markdown_link_keeps_label_only():
    t = clean("[자세한 내용](https://example.com/policy) 참고").text
    assert "자세한 내용" in t and "example.com" not in t


def test_image_and_file_keep_caption_or_name_but_drop_urls():
    t = clean(sample_markdown()).text
    assert "(이미지: 충전 화면)" in t and "![" not in t
    assert "(첨부: 정책 문서.pdf)" in t and "<file" not in t
    t2 = clean(f'<video src="{SIGNED}">시연 영상</video>\n<pdf src="{SIGNED}">약관</pdf>\n<audio src="{SIGNED}">녹음</audio>').text
    assert "(동영상: 시연 영상)" in t2 and "(PDF: 약관)" in t2 and "(오디오: 녹음)" in t2


def test_mentions_keep_display_text_only():
    t = clean('담당 <mention-user url="https://www.notion.so/u123">홍길동</mention-user>, '
              '문서 <mention-page url="https://www.notion.so/p123">정책</mention-page>, '
              '기한 <mention-date start="2026-10-16"/> / <mention-date start="2026-10-16" end="2026-10-20"/>').text
    assert "홍길동" in t and "정책" in t and "notion.so" not in t
    assert "2026-10-16" in t and "2026-10-16 ~ 2026-10-20" in t


def test_child_pages_and_databases_are_extracted_not_inlined():
    r = clean(sample_markdown())
    assert r.children == [ChildRef("01234567-89ab-cdef-0123-456789abcdef", "하위 문서")]
    assert [d.title for d in r.databases] == ["문의 목록"]
    assert "(하위 페이지: 하위 문서)" in r.text and "(데이터베이스: 문의 목록)" in r.text
    assert "notion.so" not in r.text


def test_unknown_blocks_are_removed_and_counted_by_type():
    md = '앞\n<unknown url="https://www.notion.so/x#aa" alt="bookmark"/>\n<unknown url="u" alt="embed"/>\n<unknown url="u2" alt="bookmark"/>\n뒤\n<table_of_contents/>\n<custom-block definition-id="abc" />'
    r = clean(md)
    assert r.unknown_count == 3 and r.unknown_by_type == {"bookmark": 2, "embed": 1}
    assert "<unknown" not in r.text and "<table_of_contents" not in r.text and "<custom-block" not in r.text
    assert "앞" in r.text and "뒤" in r.text


def test_code_blocks_and_inline_code_are_left_alone():
    md = "설명\n\n```html\n<callout>keep me</callout>\n<div>x</div>\n```\n\n인라인 `<page url=\"https://www.notion.so/" + "a" * 32 + "\">t</page>` 끝"
    r = clean(md)
    assert "<callout>keep me</callout>" in r.text and "<div>x</div>" in r.text
    assert '`<page url="' in r.text
    assert r.children == []  # 코드 안의 태그는 자식 문서가 아니다


def test_unrecognized_tags_are_kept_but_counted_so_format_drift_is_visible():
    r = clean("본문 <newtag foo=bar>x</newtag> 끝")
    assert "<newtag" in r.text and r.unrecognized_tags.get("newtag", 0) >= 1


def test_whitespace_is_normalized():
    t = clean("a  \n\n\n\n\tb\t\n   \n\n\nc  ").text
    assert "\n\n\n" not in t and "\t" not in t and not re.search(r" +\n", t) and t == t.strip()


# ---------- 서명 URL 제거 (보안) ----------

def test_signed_urls_are_removed_everywhere_in_the_sample():
    assert not SIGNED_MARKERS.search(clean(sample_markdown()).text)


def _fragments(rng, url):
    return [
        f"![{rng.choice(['그림', ''])}]({url})",
        f"[링크]({url})",
        f'<file src="{url}">첨부.pdf</file>',
        f'<video src="{url}">영상</video>',
        f'<pdf src="{url}"></pdf>',
        f'<audio src="{url}">소리</audio>',
        f"그냥 본문에 {url} 이 들어간 문장",
        f"인라인 코드 `{url}` 입니다",
        f"```\n{url}\n```",
        f"<table><tr><td>셀 {url}</td><td>b</td></tr></table>",
        f'<unknown url="{url}" alt="embed"/>',
        f'<callout>\n\t{url}\n</callout>',
    ]


def test_property_no_signed_url_survives_any_context():
    rng = random.Random(1234)
    urls = [
        SIGNED,
        "https://file.notion.so/f/f/abc/def/img.png?table=block&id=1&expirationTimestamp=1&signature=zzz",
        "https://s3.us-west-2.amazonaws.com/secure.notion-static.com/abc/img.png?X-Amz-Algorithm=A&X-Amz-Signature=s",
        "https://example.com/x?X-Amz-Signature=abc&X-Amz-Credential=c",
    ]
    for _ in range(300):
        parts = [rng.choice(_fragments(rng, rng.choice(urls))) for _ in range(rng.randint(1, 6))]
        text = clean("\n\n".join(parts)).text
        assert not SIGNED_MARKERS.search(text), text


# ---------- 멱등 / 해시 ----------

def test_cleaning_is_idempotent():
    once = clean(sample_markdown()).text
    assert clean(once).text == once


def test_hash_is_stable_when_only_the_signed_url_differs():
    a = content_hash(clean(sample_markdown(SIGNED)).text)
    b = content_hash(clean(sample_markdown(SIGNED_OTHER)).text)
    assert a == b


def test_hash_changes_when_content_changes_and_ignores_line_endings():
    base = clean("# 제목\n본문").text
    assert content_hash(base) != content_hash(clean("# 제목\n본문 수정").text)
    assert content_hash("a\nb") == content_hash("a\r\nb")


@pytest.mark.parametrize("md", ["", "   \n\n", "# 제목만", "<unknown url='u' alt='x'/>"])
def test_degenerate_inputs_do_not_crash(md):
    r = clean(md)
    assert isinstance(r.text, str)
