"""동기화 테스트용 대역: 메모리 저장소와 가짜 임베딩 서버."""

from __future__ import annotations

from notion_ingest.embed_client import EmbedBatch
from notion_ingest.store import StoredDoc
from notion_ingest.timeutil import normalize_time, parse_time


class FakeStore:
    def __init__(self):
        self.docs: dict[str, StoredDoc] = {}
        self.chunks: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self.runs: dict[str, dict] = {}
        self.locked_by_other = False
        self.fail_replace: dict[str, Exception] = {}
        self.fail_state: Exception | None = None
        self.fail_finish: Exception | None = None
        self.closed = False

    # --- Store 인터페이스 ---
    def try_lock(self) -> bool:
        self.calls.append(("lock",))
        return not self.locked_by_other

    def start_run(self) -> str:
        self.calls.append(("start_run",))
        run_id = f"run-{len(self.runs) + 1}"
        self.runs[run_id] = {"status": "running"}
        return run_id

    def get_sync_state(self) -> dict[str, StoredDoc]:
        self.calls.append(("state",))
        if self.fail_state:
            raise self.fail_state
        return dict(self.docs)

    def replace_document(self, *, page_id, title, url, breadcrumb, last_edited_time, content_hash, index_signature, chunks):
        self.calls.append(("replace", page_id))
        if page_id in self.fail_replace:
            raise self.fail_replace[page_id]
        parsed = parse_time(last_edited_time)
        # DB 가 돌려주는 표기(+00:00)를 흉내 내서 시각 형식 정규화가 실제로 필요하게 만든다
        stored_time = parsed.isoformat() if parsed else None
        self.docs[page_id] = StoredDoc(stored_time, content_hash, title, breadcrumb, index_signature)
        self.chunks[page_id] = [dict(c) for c in chunks]

    def update_metadata(self, *, page_id, title, url, breadcrumb, last_edited_time) -> bool:
        self.calls.append(("update_meta", page_id))
        old = self.docs.get(page_id)
        if old is None:
            return False
        parsed = parse_time(last_edited_time)
        self.docs[page_id] = StoredDoc(parsed.isoformat() if parsed else None, old.content_hash, title, breadcrumb, old.index_signature)
        return True

    def delete_document(self, page_id: str) -> bool:
        self.calls.append(("delete", page_id))
        self.chunks.pop(page_id, None)
        return self.docs.pop(page_id, None) is not None

    def delete_documents_not_in(self, keep_page_ids: list[str]) -> int:
        self.calls.append(("delete_not_in", tuple(keep_page_ids)))
        if not keep_page_ids:
            raise RuntimeError("refusing to delete all")
        gone = [p for p in self.docs if p not in keep_page_ids]
        for page_id in gone:
            self.docs.pop(page_id)
            self.chunks.pop(page_id, None)
        return len(gone)

    def finish_run(self, run_id, status, counts, reasons) -> None:
        self.calls.append(("finish_run", run_id))
        if self.fail_finish:
            raise self.fail_finish
        self.runs[run_id] = {"status": status, "counts": dict(counts), "reasons": list(reasons)}

    def close(self) -> None:
        self.closed = True

    # --- 검증 도우미 ---
    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]

    def replaced(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "replace"]

    def reset_calls(self) -> None:
        self.calls.clear()

    def stored_time(self, page_id: str) -> str | None:
        return normalize_time(self.docs[page_id].last_edited_time)


class FakeEmbedder:
    """토큰 수는 입력 글자 수로 가정한다. truncate_over 보다 긴 입력은 truncated 로 답한다."""

    def __init__(self, revision: str = "rev-1", model: str = "fake-kure", truncate_over: int | None = None):
        self.revision, self.model, self.truncate_over = revision, model, truncate_over
        self.probe_error: Exception | None = None
        self.embed_revision: str | None = None  # probe 와 다른 revision 으로 응답하게 만든다(실행 중 모델 교체 흉내)
        self.errors: list[Exception | None] = []  # embed 호출마다 하나씩 소비(None 이면 정상)
        self.calls: list[list[str]] = []
        self.probes = 0

    def probe(self) -> tuple[str, str]:
        self.probes += 1
        if self.probe_error:
            raise self.probe_error
        return self.model, self.revision

    def embed(self, texts: list[str]) -> EmbedBatch:
        self.calls.append(list(texts))
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        return EmbedBatch(
            vectors=[[float(len(t)), 1.0, 0.0, 0.0] for t in texts],
            truncated=[self.truncate_over is not None and len(t) > self.truncate_over for t in texts],
            token_counts=[len(t) for t in texts],
            model=self.model,
            revision=self.embed_revision or self.revision,
        )

    def embedded_texts(self) -> list[str]:
        return [t for call in self.calls for t in call]
