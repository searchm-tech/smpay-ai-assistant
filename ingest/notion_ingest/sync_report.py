"""동기화 결과 출력. 문서 id·개수·오류 종류만 담고 제목·본문은 담지 않는다."""

from __future__ import annotations

from notion_ingest.sync import SyncSummary

STATUS_LABEL = {"complete": "완전 성공", "partial": "일부 완료", "failed": "실패"}
SECONDS_PER_CHUNK = 1.5  # A2 측정(맥북 Intel, 500자 청크). 데스크탑 수치는 B1에서 다시 잰다

HINTS = {
    "notion_auth": "Notion 토큰과 통합의 'Read content' 권한, 루트 페이지에 통합이 연결되어 있는지 확인하세요",
    "notion_error": "Notion 호출이 실패했습니다. 잠시 뒤 다시 실행하세요",
    "embedder_unavailable": "임베딩 서버(smpay-kure-model)가 떠 있는지, EMBEDDER_URL 을 확인하세요",
    "embedder_auth": "EMBEDDER_SECRET 이 임베딩 서버의 값과 같은지 확인하세요",
    "revision_changed": "실행 중 임베딩 모델이 바뀌었습니다. 서버 상태를 확인한 뒤 다시 실행하세요",
    "locked": "다른 동기화가 실행 중입니다(맥북 테스트와 데스크탑 운영을 동시에 돌리지 마세요)",
    "delete_guard": "삭제 대상이 많아 거부했습니다. 의도한 변경이면 확인 후 --force-delete 로 다시 실행하세요",
    "store_error": "DB 연결·권한을 확인하세요",
    "doc_failures": "일부 문서가 실패했습니다. 아래 목록과 로그의 오류 종류를 확인하세요",
    "limited": "--limit 으로 일부만 처리했습니다. 다시 실행하면 이어서 처리됩니다",
    "interrupted": "중단되었습니다. 다시 실행하면 이어서 처리됩니다",
    "internal_error": "예상하지 못한 오류입니다. 로그를 확인하세요",
}


def _hint(reason: str) -> str | None:
    if reason.startswith("crawl_"):
        return "Notion 수집이 불완전해 삭제를 건너뛰었습니다(접근 오류·페이지 수 제한 등)"
    return HINTS.get(reason)


def summary_to_dict(summary: SyncSummary) -> dict:
    return {
        "status": summary.status,
        "exit_code": summary.exit_code,
        "dry_run": summary.dry_run,
        "counts": dict(summary.counts),
        "reasons": list(summary.reasons),
        "failed": [{"page_id": page_id, "kind": kind} for page_id, kind in summary.failed],
        "quarantined": [{"page_id": page_id, "patterns": list(patterns)} for page_id, patterns in summary.quarantined],
        "deferred": summary.deferred,
        "partial_docs": summary.partial_docs,
        "estimated_chunks": summary.estimated_chunks,
        "delete_candidates": summary.delete_candidates,
    }


def format_summary(summary: SyncSummary) -> str:
    c = summary.counts
    title = "동기화 계획 (dry-run: 임베딩·DB 쓰기 없음)" if summary.dry_run else "동기화 결과"
    lines = [
        f"{title}: {STATUS_LABEL.get(summary.status, summary.status)} ({summary.status})",
        f"  신규 {c['new']} · 변경 {c['changed']} · 메타만 {c['metadata_only']} · 변경 없음 {c['unchanged']}"
        f" · 삭제 {c['deleted']} · 격리 {c['quarantined']} · 실패 {c['failed']}",
    ]
    if summary.deferred:
        lines.append(f"  미처리 {summary.deferred} (다음 실행에서 이어서 처리)")
    if summary.partial_docs:
        lines.append(f"  내용이 일부만 수집된 문서 {summary.partial_docs}")
    if summary.dry_run:
        minutes = summary.estimated_chunks * SECONDS_PER_CHUNK / 60
        lines.append(
            f"  예상 청크 {summary.estimated_chunks}개 → 임베딩 약 {minutes:.0f}분 (청크당 {SECONDS_PER_CHUNK}초 기준)"
        )
        lines.append(f"  삭제 후보 {summary.delete_candidates} (실제 실행 시 가드 한도를 넘으면 --force-delete 필요)")
    if summary.reasons:
        lines.append("  사유:")
        for reason in summary.reasons:
            hint = _hint(reason)
            lines.append(f"    - {reason}" + (f": {hint}" if hint else ""))
    if summary.quarantined:
        lines.append("  격리된 문서(민감정보 패턴이 보여 색인하지 않음; 확인 후 INGEST_ALLOW_SENSITIVE_PAGE_IDS 로 허용):")
        lines += [f"    {page_id}  ({', '.join(patterns)})" for page_id, patterns in summary.quarantined]
    if summary.failed:
        lines.append("  실패한 문서:")
        lines += [f"    {page_id}  ({kind})" for page_id, kind in summary.failed]
    return "\n".join(lines)
