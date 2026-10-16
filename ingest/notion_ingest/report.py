"""측정 리포트: 문서 본문과 매칭된 값은 담지 않고 개수·분포만 담는다 (채팅에 붙여넣어도 안전)."""

from __future__ import annotations

import math
from collections import Counter

from notion_ingest.crawler import CrawlResult

CHUNK_SIZES = (500, 800, 1200)


def percentile(values: list[int], p: float) -> int:
    """nearest-rank 방식."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(math.ceil(p / 100 * len(ordered)), 1) - 1]


def build_report(result: CrawlResult, include_titles: bool = True) -> dict:
    docs = result.documents
    chars = [d.char_count for d in docs]

    unknown_by_type: Counter = Counter()
    sensitive_totals: Counter = Counter()
    sensitive_pages = []
    for d in docs:
        unknown_by_type.update(d.unknown_by_type)
        if d.sensitive_hits:
            sensitive_totals.update(d.sensitive_hits)
            page = {"page_id": d.page_id, "hits": dict(d.sensitive_hits)}
            if include_titles:
                page.update(title=d.title, url=d.url)
            sensitive_pages.append(page)

    databases = {"count": len(result.databases_skipped)}
    if include_titles:
        databases["titles"] = [db.title for db in result.databases_skipped]

    return {
        "documents": len(docs),
        "by_status": dict(Counter(d.status for d in docs)),
        "complete": result.complete,
        "incomplete_reasons": list(result.incomplete_reasons),
        "depth": {"max": max((d.depth for d in docs), default=0), "counts": dict(sorted(Counter(d.depth for d in docs).items()))},
        "chars": {
            "total": sum(chars),
            "p50": percentile(chars, 50),
            "p90": percentile(chars, 90),
            "p99": percentile(chars, 99),
            "max": max(chars, default=0),
        },
        # 청크 크기별 예상 청크 수 — A6의 청크 크기와 A4에서 보류한 인덱스 도입 여부를 정하는 입력
        "estimated_chunks": {
            str(size): sum(math.ceil(c / size) for c in chars if c > 0) for size in CHUNK_SIZES
        },
        "truncated_pages": sum(d.truncated for d in docs),
        "partial_pages": sum(d.partial for d in docs),
        "unknown_blocks": sum(d.unknown_count for d in docs),
        "unknown_by_type": dict(unknown_by_type),
        "tables": sum(d.table_count for d in docs),
        "databases_skipped": databases,
        "foreign_links_skipped": result.foreign_links_skipped,
        "unverified_children_skipped": result.unverified_children_skipped,
        "inaccessible_children": result.inaccessible_children,
        "errors": dict(Counter(kind for _, kind in result.errors)),
        "sensitive": {
            "pages_with_hits": len(sensitive_pages),
            "totals": dict(sensitive_totals),
            "pages": sensitive_pages,
        },
    }


def format_report(report: dict) -> str:
    chars, est, sens = report["chars"], report["estimated_chunks"], report["sensitive"]
    lines = [
        f"수집 문서 {report['documents']}개 (상태: {report['by_status']}), 최대 깊이 {report['depth']['max']}",
        f"수집 완전성: {'완전' if report['complete'] else '불완전 — ' + ', '.join(report['incomplete_reasons'])}",
        f"글자 수: 합계 {chars['total']:,} / p50 {chars['p50']:,} / p90 {chars['p90']:,} / p99 {chars['p99']:,} / 최대 {chars['max']:,}",
        "예상 청크 수: " + ", ".join(f"{size}자 기준 {n:,}개" for size, n in est.items()),
        f"잘린 페이지 {report['truncated_pages']} / 부분 수집 {report['partial_pages']} / 미지원·접근불가 블록 {report['unknown_blocks']} {report['unknown_by_type']}",
        f"표 {report['tables']}개, 건너뛴 DB {report['databases_skipped']['count']}개, "
        f"허용 목록 밖 링크 {report['foreign_links_skipped']}, 부모 미확인 {report['unverified_children_skipped']}, 접근 불가 하위 {report['inaccessible_children']}",
        f"오류: {report['errors'] or '없음'}",
        f"민감정보 패턴이 보인 페이지 {sens['pages_with_hits']}개 {sens['totals']}",
    ]
    for page in sens["pages"]:
        label = f"{page.get('title', '')} {page.get('url', '')}".strip() or page["page_id"]
        lines.append(f"  - {label}: {page['hits']}")
    return "\n".join(lines)
