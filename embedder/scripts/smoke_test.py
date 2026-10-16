"""KURE-v1 스모크 테스트. 네트워크 없이 로컬에 받아둔 모델만 쓴다.

검증: 로딩 / 차원 / 한국어 유사도 순서 / 배치 일관성 / 긴 입력 처리 / 속도·메모리.
실행: embedder 폴더에서  HF_HUB_OFFLINE=1 .venv/bin/python scripts/smoke_test.py
"""

import os
import resource
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from huggingface_hub import snapshot_download  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402

from kure_config import CACHE_DIR, EMBEDDING_DIM, REPO_ID, REVISION  # noqa: E402


def rss_mb() -> float:
    # macOS는 bytes, Linux는 KB 단위
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
    if not ok:
        sys.exit(1)


def main() -> None:
    path = snapshot_download(
        repo_id=REPO_ID, revision=REVISION, cache_dir=str(CACHE_DIR), local_files_only=True
    )
    t0 = time.time()
    model = SentenceTransformer(path, device="cpu", trust_remote_code=False)
    check("1. 로컬 모델 로딩(오프라인)", True, f"{time.time() - t0:.1f}s, max_seq_length={model.max_seq_length}")

    query = "광고비 충전 한도를 늘리려면 어떻게 해야 하나요?"
    related = "광고비 충전 한도 변경 방법과 신청 절차를 안내합니다."
    unrelated = "이번 주 사무실 점심 메뉴로 추천하는 맛집 목록입니다."
    emb = model.encode([query, related, unrelated], normalize_embeddings=True)

    check("2. 임베딩 차원", emb.shape == (3, EMBEDDING_DIM), f"shape={emb.shape}")
    check("2-1. 정규화(길이 1)", bool(np.allclose(np.linalg.norm(emb, axis=1), 1.0, atol=1e-4)))
    check("2-2. 값이 유한(NaN/inf 없음)", bool(np.isfinite(emb).all()))

    s_rel, s_unrel = float(emb[0] @ emb[1]), float(emb[0] @ emb[2])
    check(
        "3. 한국어 유사도 순서(관련 > 무관, 차이 0.1 이상)",
        s_rel > s_unrel + 0.1,
        f"관련={s_rel:.3f}, 무관={s_unrel:.3f}",
    )

    # 배치 안에서 패딩 때문에 값이 달라지지 않는지 (짧은 글 + 긴 글 섞어 인코딩)
    alone = model.encode([query], normalize_embeddings=True)[0]
    mixed = model.encode([query, "가" * 400, query + " " + "나다라 " * 50], normalize_embeddings=True)[0]
    cos = float(alone @ mixed)
    check("4. 배치 일관성(단독 vs 혼합 배치, cos≥0.999)", cos >= 0.999, f"cos={cos:.5f}")

    long_text = "충전 한도 안내 문장입니다. " * 2000  # 모델 최대 길이를 훌쩍 넘김
    e_long = model.encode([long_text], normalize_embeddings=True)
    check("5. 매우 긴 입력은 오류 없이 잘려서 처리", e_long.shape == (1, EMBEDDING_DIM))

    check("6. 빈 문자열도 오류 없이 처리", model.encode([""], normalize_embeddings=True).shape == (1, EMBEDDING_DIM))

    # 속도: 질문 1건(요청마다 발생), 청크 16개 배치(백필)
    model.encode([query])  # 워밍업
    t = time.time()
    for _ in range(5):
        model.encode([query], normalize_embeddings=True)
    q_ms = (time.time() - t) / 5 * 1000
    chunks = ["광고비 충전 한도와 자동 충전 규칙에 대한 안내 문서의 일부입니다. " * 12] * 16  # 약 500자
    t = time.time()
    model.encode(chunks, batch_size=16, normalize_embeddings=True)
    b_s = time.time() - t
    print(f"[INFO] 질문 1건 평균 {q_ms:.0f}ms | 500자 청크 16개 배치 {b_s:.1f}s ({b_s / 16 * 1000:.0f}ms/건)")
    print(f"[INFO] 최대 메모리(RSS) 약 {rss_mb():.0f}MB")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
