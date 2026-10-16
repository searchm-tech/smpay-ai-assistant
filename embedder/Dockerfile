# smpay-kure-model: KURE-v1 임베딩 서비스 컨테이너 (linux/amd64, CPU 전용)
#
# 모델 가중치(약 2.1GB)는 이미지에 넣지 않고 실행 시 읽기 전용으로 마운트한다(scripts/docker_run.sh).
# 비밀값(EMBEDDER_SECRET)도 이미지에 넣지 않는다. 실행 시 --env-file 로만 전달한다.

FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 로컬(맥북)과 같은 uv 버전. PyPI에서만 받는다.
RUN pip install --no-cache-dir uv==0.13.1

WORKDIR /app
COPY pyproject.toml uv.lock ./
# uv.lock 그대로 설치(재해석 금지). 개발 의존성(pytest 등)은 제외한다.
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/hf

# 비루트 사용자. 읽기 전용 루트 파일시스템에서도 돌도록 쓰기가 필요한 곳은 /tmp(tmpfs)뿐이다.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin kure

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY kure_config.py ./

USER 10001:10001
EXPOSE 8001

# 모델 로딩에 시간이 걸리므로 start-period 를 넉넉히 둔다. /health 는 인증이 없다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3)"]

# 컨테이너 안에서는 0.0.0.0 이지만, 호스트로는 127.0.0.1 에만 publish 한다(scripts/docker_run.sh).
# 워커는 1개(모델 2.3GB 복제 방지).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
