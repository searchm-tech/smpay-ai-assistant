"""KURE-v1 모델 설정. 맥북과 Windows 데스크탑이 같은 모델을 쓰도록 커밋 해시로 고정한다."""

from pathlib import Path

REPO_ID = "nlpai-lab/KURE-v1"
# 2026-10-16 기준 확인한 커밋. 원본이 바뀌어도 이 버전만 받는다.
REVISION = "8b418a58414668e75532ed045c22d9ca018ae2b2"
EMBEDDING_DIM = 1024

# 모델 캐시는 git에 올리지 않는다(.gitignore의 .cache/). 각 머신에서 scripts/download_model.py로 받는다.
CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "huggingface"
