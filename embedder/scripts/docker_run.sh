#!/usr/bin/env bash
# smpay-kure-model 컨테이너 실행. 모델 캐시는 읽기 전용 마운트, 비밀값은 .env 에서만 읽는다.
# 사용: cd embedder && docker build -t smpay-kure-model . && scripts/docker_run.sh
set -euo pipefail
cd "$(dirname "$0")/.."

NAME=smpay-kure-model
[ -f .env ] || { echo ".env 가 없습니다. .env.example 을 참고해 EMBEDDER_SECRET 을 만드세요." >&2; exit 1; }
[ -d .cache/huggingface ] || { echo "모델 캐시가 없습니다. scripts/download_model.py 를 먼저 실행하세요." >&2; exit 1; }

docker rm -f "$NAME" >/dev/null 2>&1 || true

# -p 127.0.0.1:...  : 이 컴퓨터에서만 접근(LAN·외부 차단). 외부 공개는 터널이 맡는다.
# --read-only/--tmpfs: 루트 파일시스템 쓰기 금지(쓰기는 /tmp 메모리 디스크만)
# --cap-drop ALL / no-new-privileges : 리눅스 권한 제거
docker run -d --name "$NAME" \
  --env-file .env \
  -p 127.0.0.1:8001:8001 \
  -v "$PWD/.cache/huggingface:/app/.cache/huggingface:ro" \
  --read-only --tmpfs /tmp:rw,size=256m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 4g --pids-limit 256 \
  --restart on-failure:3 \
  "$NAME"
