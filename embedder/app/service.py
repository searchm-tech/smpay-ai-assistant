from __future__ import annotations

import os
from typing import Protocol

import numpy as np

from app.settings import MAX_TOKENS
from kure_config import CACHE_DIR, EMBEDDING_DIM, REPO_ID, REVISION

# 서비스는 외부로 나가는 네트워크 호출 없이 로컬 캐시의 모델만 쓴다.
os.environ.setdefault("HF_HUB_OFFLINE", "1")


class EmbeddingService(Protocol):
    model_name: str
    revision: str
    dim: int

    def embed(self, texts: list[str]) -> tuple[np.ndarray, list[bool]]:
        """(정규화된 임베딩 (n, dim), 입력별 잘림 여부)를 입력 순서대로 반환한다."""
        ...


class KureEmbeddingService:
    model_name = REPO_ID
    revision = REVISION
    dim = EMBEDDING_DIM

    def __init__(self, model) -> None:
        self._model = model
        self._model.max_seq_length = MAX_TOKENS

    @classmethod
    def load(cls) -> "KureEmbeddingService":
        # torch 등 무거운 import는 실제로 모델을 올릴 때만 한다 (가짜 서비스 테스트를 빠르게 유지).
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer

        try:
            path = snapshot_download(
                repo_id=REPO_ID, revision=REVISION, cache_dir=str(CACHE_DIR), local_files_only=True
            )
        except Exception:
            raise RuntimeError(
                "KURE-v1 model cache not found. Run `python scripts/download_model.py` first."
            ) from None

        service = cls(SentenceTransformer(path, device="cpu", trust_remote_code=False))
        service.embed(["워밍업"])  # 첫 요청이 느리지 않도록 한 번 돌려둔다
        return service

    def embed(self, texts: list[str]) -> tuple[np.ndarray, list[bool]]:
        ids = self._model.tokenizer(texts, add_special_tokens=True, truncation=False)["input_ids"]
        truncated = [len(token_ids) > MAX_TOKENS for token_ids in ids]
        vectors = self._model.encode(
            texts,
            batch_size=16,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors, truncated
