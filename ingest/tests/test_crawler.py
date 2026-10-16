import pytest

from notion_ingest.cleaner import clean_markdown, content_hash
from notion_ingest.crawler import PrevDoc, crawl
from notion_ingest.notion_client import NotionAuthError, NotionError, NotionNotFound
from tests.conftest import child_tag
from tests.fixtures import pid


def build_tree(fake):
    fake.add(1, "루트", "# 루트\n" + child_tag(2, "A") + "\n" + child_tag(3, "B"))
    fake.add(2, "A", "# A\n" + child_tag(4, "A1"), parent=1)
    fake.add(3, "B", "# B", parent=1)
    fake.add(4, "A1", "# A1", parent=2)


def titles(result):
    return [d.title for d in result.documents]


def prev_for(fake, n, **override):
    p = fake.pages[pid(n)]
    base = dict(last_edited_time=p["last_edited"], content_hash=content_hash(clean_markdown(p["markdown"]).text),
                title=p["title"], breadcrumb="")
    base.update(override)
    return PrevDoc(**base)


# ---------- 순회와 허용 목록 ----------

def test_walks_the_tree_breadth_first_with_breadcrumbs_and_depth(fake):
    build_tree(fake)
    r = crawl(fake, [pid(1)])
    assert titles(r) == ["루트", "A", "B", "A1"]
    a1 = next(d for d in r.documents if d.title == "A1")
    assert a1.breadcrumb == ["루트", "A"] and a1.depth == 2 and a1.status == "new" and a1.text
    assert r.complete and r.errors == []


def test_each_page_costs_exactly_one_meta_and_one_markdown_call(fake):
    build_tree(fake)
    crawl(fake, [pid(1)])
    assert fake.count("page") == 4 and fake.count("markdown") == 4


def test_pages_outside_the_allowlist_are_never_fetched(fake):
    build_tree(fake)
    fake.add(9, "무관한 문서", "# 다른 곳", parent=7)
    fake.add(7, "다른 루트", "# 다른 루트 " + child_tag(9, "무관한 문서"))
    crawl(fake, [pid(1)])
    assert not fake.touched(pid(9)) and not fake.touched(pid(7))


def test_a_link_to_a_foreign_page_is_not_followed_even_though_the_tag_looks_like_a_child(fake):
    # link_to_page 블록이 허용 목록 밖 페이지를 가리켜도, 실제 부모가 이 페이지가 아니면 수집하지 않는다.
    fake.add(1, "루트", "# 루트\n" + child_tag(9, "비공개"))
    fake.add(9, "비공개", "# 비밀 내용", parent=7)
    r = crawl(fake, [pid(1)])
    assert titles(r) == ["루트"] and r.foreign_links_skipped == 1
    assert fake.count("markdown", pid(9)) == 0  # 본문은 가져오지 않는다


def test_children_whose_parent_cannot_be_verified_are_skipped_and_counted(fake):
    fake.add(1, "루트", "# 루트\n" + child_tag(2, "블록 안 페이지"))
    fake.add(2, "블록 안 페이지", "# x", parent=1, parent_type="block_id")
    r = crawl(fake, [pid(1)])
    assert titles(r) == ["루트"] and r.unverified_children_skipped == 1 and fake.count("markdown", pid(2)) == 0


def test_databases_are_skipped_and_reported_not_fetched(fake):
    fake.add(1, "루트", f'# 루트\n<database url="https://www.notion.so/DB-{5:032x}">문의 목록</database>')
    r = crawl(fake, [pid(1)])
    assert [d.title for d in r.databases_skipped] == ["문의 목록"] and not fake.touched(pid(5))


def test_cycles_and_duplicate_links_are_processed_once(fake):
    fake.add(1, "루트", "# 루트\n" + child_tag(2, "A") + child_tag(2, "A"))
    fake.add(2, "A", "# A\n" + child_tag(1, "루트"), parent=1)
    r = crawl(fake, [pid(1), pid(1)])
    assert titles(r) == ["루트", "A"] and fake.count("markdown", pid(1)) == 1


def test_trashed_pages_are_skipped(fake):
    fake.add(1, "루트", "# 루트\n" + child_tag(2, "A") + child_tag(3, "휴지통"))
    fake.add(2, "A", "# A", parent=1)
    fake.add(3, "휴지통", "# t", parent=1, in_trash=True)
    r = crawl(fake, [pid(1)], previous={pid(3): prev_for(fake, 3)})
    assert titles(r) == ["루트", "A"] and r.complete and r.deletable_ids == [pid(3)]


# ---------- 가드 ----------

def test_max_pages_stops_the_crawl_and_marks_it_incomplete(fake):
    build_tree(fake)
    r = crawl(fake, [pid(1)], max_pages=2)
    assert len(r.documents) == 2 and not r.complete and "max_pages" in r.incomplete_reasons


def test_max_depth_skips_deeper_pages_and_marks_it_incomplete(fake):
    build_tree(fake)
    r = crawl(fake, [pid(1)], max_depth=1)
    assert "A1" not in titles(r) and not r.complete and "max_depth" in r.incomplete_reasons


# ---------- 오류와 완전성 ----------

def test_root_not_found_is_incomplete_so_nothing_gets_deleted(fake):
    r = crawl(fake, [pid(55)], previous={pid(77): PrevDoc("t", "h", "옛 문서", "")})
    assert (pid(55), "root_not_found") in r.errors and not r.complete
    assert "root_not_found" in r.incomplete_reasons and r.deletable_ids == []


def test_a_missing_child_is_a_legitimate_removal_not_an_incomplete_crawl(fake):
    fake.add(1, "루트", "# 루트\n" + child_tag(2, "사라진 문서"))  # 2는 존재하지 않음(접근 불가)
    r = crawl(fake, [pid(1)], previous={pid(2): PrevDoc("t", "h", "사라진 문서", "루트")})
    assert r.complete and r.inaccessible_children == 1 and r.deletable_ids == [pid(2)]


def test_transient_failure_marks_incomplete_blocks_deletion_and_skips_that_subtree(fake):
    build_tree(fake)
    fake.page_errors[pid(2)] = NotionError("http_503", 503)
    r = crawl(fake, [pid(1)], previous={pid(77): PrevDoc("t", "h", "옛 문서", "")})
    assert (pid(2), "http_503") in r.errors and not r.complete and r.deletable_ids == []
    assert not fake.touched(pid(4))  # 실패한 페이지의 하위는 알 수 없으므로 크롤하지 않는다
    assert "B" in titles(r)  # 다른 가지는 계속 진행


def test_markdown_failure_is_recorded_per_page_and_incomplete(fake):
    build_tree(fake)
    fake.markdown_errors[pid(3)] = NotionNotFound("not_found", 404)
    r = crawl(fake, [pid(1)])
    assert (pid(3), "markdown_not_found") in r.errors and not r.complete and "B" not in titles(r)


def test_auth_errors_abort_the_whole_run(fake):
    build_tree(fake)
    fake.page_errors[pid(1)] = NotionAuthError("unauthorized", 401)
    with pytest.raises(NotionAuthError):
        crawl(fake, [pid(1)])


def test_deletable_ids_are_previous_documents_not_seen_when_complete(fake):
    build_tree(fake)
    r = crawl(fake, [pid(1)], previous={pid(77): PrevDoc("t", "h", "옛 문서", ""), pid(3): prev_for(fake, 3)})
    assert r.complete and r.deletable_ids == [pid(77)]


# ---------- 변경 감지 ----------

def test_status_is_unchanged_when_hash_title_breadcrumb_and_time_match(fake):
    build_tree(fake)
    r = crawl(fake, [pid(1)], previous={pid(3): prev_for(fake, 3, breadcrumb="루트")})
    b = next(d for d in r.documents if d.title == "B")
    assert b.status == "unchanged" and b.text is None


def test_status_is_changed_when_content_hash_differs(fake):
    build_tree(fake)
    r = crawl(fake, [pid(1)], previous={pid(3): prev_for(fake, 3, breadcrumb="루트", content_hash="0" * 64)})
    b = next(d for d in r.documents if d.title == "B")
    assert b.status == "changed" and b.text


@pytest.mark.parametrize("override", [{"last_edited_time": "2026-09-01T00:00:00.000Z"}, {"title": "옛 제목"}, {"breadcrumb": "옛 부모"}])
def test_status_is_metadata_only_when_only_metadata_differs_so_no_reembedding(fake, override):
    build_tree(fake)
    r = crawl(fake, [pid(1)], previous={pid(3): prev_for(fake, 3, **{"breadcrumb": "루트", **override})})
    b = next(d for d in r.documents if d.title == "B")
    assert b.status == "metadata_only" and b.text is None


# ---------- 잘림(truncated) 후속 조회 ----------

def test_truncated_pages_fetch_unknown_subtrees_and_ignore_inaccessible_ones(fake):
    fake.add(1, "루트", '# 루트\n앞부분\n<unknown url="u" alt="block"/>', truncated=True, unknown_ids=[pid(21), pid(22)])
    fake.subtrees[pid(21)] = "# 뒷부분\n" + child_tag(5, "C")
    fake.add(5, "C", "# C", parent=1)
    r = crawl(fake, [pid(1)])
    root = r.documents[0]
    assert "앞부분" in root.text and "뒷부분" in root.text and root.truncated and not root.partial
    assert fake.count("markdown", pid(21)) == 1 and fake.count("markdown", pid(22)) == 1  # 22는 404로 무시됨
    assert "C" in titles(r) and r.complete


def test_unknown_blocks_are_not_followed_up_unless_the_page_was_truncated(fake):
    fake.add(1, "루트", '# 루트\n<unknown url="u" alt="bookmark"/>', truncated=False, unknown_ids=[pid(21)])
    crawl(fake, [pid(1)])
    assert not fake.touched(pid(21))


def test_followups_are_capped_and_the_page_is_flagged_partial(fake):
    ids = [pid(30 + i) for i in range(5)]
    fake.add(1, "루트", "# 루트", truncated=True, unknown_ids=ids)
    for i in ids:
        fake.subtrees[i] = "조각"
    r = crawl(fake, [pid(1)], max_followups=2)
    assert r.documents[0].partial and sum(fake.count("markdown", i) for i in ids) == 2


# ---------- 부가 정보 ----------

def test_sensitive_hits_are_recorded_per_document_as_counts(fake):
    fake.add(1, "루트", "# 루트\n문의: 02-123-4567, test@example.com")
    doc = crawl(fake, [pid(1)]).documents[0]
    assert doc.sensitive_hits.get("email") == 1 and doc.sensitive_hits.get("phone_kr") == 1
    assert doc.char_count == len(doc.text)
