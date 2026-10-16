import json

import pytest

from notion_ingest.crawler import crawl
from notion_ingest.report import build_report, format_report
from notion_ingest.scanner import scan_sensitive
from tests.conftest import child_tag
from tests.fixtures import SENTINEL, SIGNED, pid


# ---------- 스캐너 ----------

@pytest.mark.parametrize(
    "text,key",
    [
        ("사업자번호 123-45-67890 입니다", "biz_reg_no"),
        ("주민번호 900101-1234567", "rrn"),
        ("카드 1234-5678-9012-3456 로 결제", "card"),
        ("메일 someone@example.co.kr 로", "email"),
        ("연락처 010-1234-5678 / 02-123-4567", "phone_kr"),
        ("키 sk-abcdefghijklmnopqrstuvwxyz123456", "api_key_like"),
        ("키 AKIAABCDEFGHIJKLMNOP", "api_key_like"),
        ("키 gsk_abcdefghijklmnopqrstuvwxyz", "api_key_like"),
        (f"링크 {SIGNED}", "signed_url"),
        ("비밀번호: hunter2", "secret_keyword"),
        ("password = abc", "secret_keyword"),
    ],
)
def test_each_pattern_is_detected(text, key):
    assert scan_sensitive(text).get(key, 0) >= 1


def test_clean_text_has_no_hits():
    assert scan_sensitive("# 충전 한도 안내\n광고비 충전 한도는 관리자 승인 후 변경됩니다. 한도는 1,000만 원입니다.") == {}


def test_scanner_returns_only_counts_never_the_matched_values():
    out = scan_sensitive("메일 leak@example.com 와 사업자번호 123-45-67890")
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in out.items())
    assert "leak@example.com" not in json.dumps(out) and "123-45-67890" not in json.dumps(out)


# ---------- 리포트 ----------

def make_result(fake, lengths=(100, 200, 300, 400, 1000)):
    body = "".join(child_tag(10 + i, f"문서{i}") + "\n" for i in range(len(lengths)))
    fake.add(1, "루트", "루트본문" + body)
    for i, n in enumerate(lengths):
        fake.add(10 + i, f"문서{i}", "가" * n, parent=1)
    return crawl(fake, [pid(1)])


def test_report_summarizes_counts_and_lengths(fake):
    r = make_result(fake)
    rep = build_report(r)
    assert rep["documents"] == 6 and rep["complete"] is True and rep["by_status"] == {"new": 6}
    # 루트(본문 4자 + 자식 링크 표기)를 제외하고 자식 5개만으로 분포 검증
    child_chars = sorted(d.char_count for d in r.documents if d.title != "루트")
    assert child_chars == [100, 200, 300, 400, 1000]
    assert rep["chars"]["max"] >= 1000 and rep["chars"]["p50"] <= rep["chars"]["p90"] <= rep["chars"]["p99"] <= rep["chars"]["max"]
    assert rep["depth"]["max"] == 1 and rep["depth"]["counts"] == {0: 1, 1: 5}
    assert set(rep["estimated_chunks"]) == {"500", "800", "1200"}
    assert rep["estimated_chunks"]["500"] >= rep["estimated_chunks"]["1200"] >= 6


def test_percentiles_use_nearest_rank(fake):
    fake.add(1, "루트", "")
    result = crawl(fake, [pid(1)])
    for d in result.documents:
        d.char_count = 0
    from notion_ingest.report import percentile
    assert percentile([100, 200, 300, 400, 1000], 50) == 300
    assert percentile([100, 200, 300, 400, 1000], 90) == 1000
    assert percentile([], 50) == 0


def test_report_never_contains_document_text_or_matched_values(fake):
    fake.add(1, "루트", f"# 루트\n{SENTINEL}\n문의: leak@example.com 010-1234-5678\n" + child_tag(2, "A"))
    fake.add(2, "A", f"{SENTINEL} 사업자 123-45-67890", parent=1)
    rep = build_report(crawl(fake, [pid(1)]))
    dumped = json.dumps(rep, ensure_ascii=False)
    for forbidden in (SENTINEL, "leak@example.com", "010-1234-5678", "123-45-67890"):
        assert forbidden not in dumped and forbidden not in format_report(rep)
    assert rep["sensitive"]["pages_with_hits"] == 2
    assert {"email", "phone_kr", "biz_reg_no"} <= set(rep["sensitive"]["totals"])
    assert all("hits" in p and "text" not in p for p in rep["sensitive"]["pages"])


def test_report_counts_skips_unknowns_errors_and_truncation(fake):
    fake.add(1, "루트", '# 루트\n<unknown url="u" alt="bookmark"/>\n<database url="https://www.notion.so/D-' + "c" * 32 + '">DB</database>\n'
             + child_tag(9, "외부"), truncated=True)
    fake.add(9, "외부", "# 외부", parent=7)
    rep = build_report(crawl(fake, [pid(1)], previous={}))
    assert rep["truncated_pages"] == 1 and rep["unknown_blocks"] == 1 and rep["unknown_by_type"] == {"bookmark": 1}
    assert rep["databases_skipped"]["count"] == 1 and rep["foreign_links_skipped"] == 1


def test_report_can_omit_titles(fake):
    fake.add(1, "비밀스러운 제목", "문의 a@b.co " + "x")
    rep = build_report(crawl(fake, [pid(1)]), include_titles=False)
    assert "비밀스러운 제목" not in json.dumps(rep, ensure_ascii=False)


def test_report_is_json_serializable_and_format_is_readable(fake):
    rep = build_report(make_result(fake))
    json.dumps(rep)
    text = format_report(rep)
    assert "문서" in text and "완전" in text
