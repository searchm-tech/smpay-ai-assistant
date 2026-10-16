"""KURE-v1을 고정된 커밋으로 내려받는다 (약 2.1GB). 이미 있으면 다시 받지 않는다.

실행: embedder 폴더에서  .venv/bin/python scripts/download_model.py   (Windows: .venv\\Scripts\\python)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from huggingface_hub import snapshot_download  # noqa: E402

from kure_config import CACHE_DIR, REPO_ID, REVISION  # noqa: E402


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_download(
        repo_id=REPO_ID,
        revision=REVISION,
        cache_dir=str(CACHE_DIR),
        # 로고 이미지는 불필요. 가중치는 safetensors만 존재(pickle 없음).
        ignore_patterns=["*.png"],
    )
    print(f"done: {path}")


if __name__ == "__main__":
    main()
