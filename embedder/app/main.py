from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.schemas import EmbedRequest, EmbedResponse
from app.service import EmbeddingService, KureEmbeddingService
from app.settings import MAX_BODY_BYTES, Settings

logger = logging.getLogger("embedder")


def _setup_logging() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def create_app(
    service: EmbeddingService | None = None, settings: Settings | None = None
) -> FastAPI:
    _setup_logging()
    settings = settings or Settings.from_env()
    # pending = 실행 중 + 대기 중 요청 수. 추론은 한 번에 1건만 돌린다(PyTorch가 이미 여러 코어를 사용).
    state = SimpleNamespace(service=service, pending=0, lock=asyncio.Lock())

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.validate()
        if state.service is None:
            state.service = await run_in_threadpool(KureEmbeddingService.load)
        yield

    # 터널로 열리는 서비스이므로 문서 화면(/docs, /openapi.json)은 끈다.
    app = FastAPI(title="smpay-embedder", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def precheck(request: Request) -> JSONResponse | None:
        """인증과 본문 크기를 본문 파싱 전에 검사한다. 인증이 가장 먼저라 내용 없이는 아무것도 알 수 없다."""
        provided = request.headers.get("x-internal-secret", "")
        if not hmac.compare_digest(provided.encode(), settings.secret.encode()):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None:
                return JSONResponse({"detail": "content-length required"}, status_code=411)
            if not length.isdigit() or int(length) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "payload too large"}, status_code=413)
        return None

    @app.middleware("http")
    async def guard_and_log(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        request.state.texts = 0
        request.state.chars = 0
        started = time.perf_counter()

        response = precheck(request) if request.url.path != "/health" else None
        if response is None:
            response = await call_next(request)
        response.headers["x-request-id"] = request_id

        # 로그에는 식별자·개수·시간·상태만 남기고 텍스트 내용은 절대 남기지 않는다.
        logger.info(
            "request_id=%s method=%s path=%s status=%s texts=%s chars=%s ms=%.0f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            request.state.texts,
            request.state.chars,
            (time.perf_counter() - started) * 1000,
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(_: Request, exc: RequestValidationError):
        # FastAPI 기본 응답은 입력값(input)을 되돌려주므로, 위치·종류·메시지만 남긴다.
        errors = [
            {"loc": list(e.get("loc", ())), "type": e.get("type"), "msg": e.get("msg")}
            for e in exc.errors()
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/embed", response_model=EmbedResponse)
    async def embed(body: EmbedRequest, request: Request):
        request.state.texts = len(body.texts)
        request.state.chars = sum(len(t) for t in body.texts)

        if state.pending >= 1 + settings.queue_limit:
            raise HTTPException(status_code=503, detail="busy", headers={"Retry-After": "2"})

        state.pending += 1
        try:
            async with state.lock:
                vectors, truncated = await run_in_threadpool(state.service.embed, body.texts)
        except Exception as e:
            # 예외 메시지에 입력 텍스트가 섞일 수 있으므로 종류만 기록하고 응답은 일반화한다.
            logger.error("request_id=%s embed_failed error=%s", request.state.request_id, type(e).__name__)
            raise HTTPException(status_code=500, detail="internal error") from None
        finally:
            state.pending -= 1

        return EmbedResponse(
            model=state.service.model_name,
            revision=state.service.revision,
            dim=state.service.dim,
            embeddings=np.round(vectors, 6).tolist(),
            truncated=list(truncated),
        )

    return app


app = create_app()
