import os
from dataclasses import dataclass, field

# 설계 문서(docs/design.md)의 A3 확정값
MIN_SECRET_LENGTH = 32
MAX_TEXTS = 32  # 요청당 텍스트 수
MAX_CHARS = 4000  # 텍스트당 글자 수 (초과는 422, 비정상 입력 방어)
MAX_TOKENS = 512  # 모델 입력 토큰 상한 (초과는 잘라서 처리하고 truncated 표시)
MAX_BODY_BYTES = 1024 * 1024  # 요청 본문 1MB


def clean_env_value(value: str) -> str:
    """환경변수에 섞이기 쉬운 앞뒤 공백과 감싼 따옴표를 정리한다."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


@dataclass(frozen=True)
class Settings:
    # repr=False: 오류 메시지·로그에 비밀값이 찍히지 않도록
    secret: str = field(repr=False)
    queue_limit: int = 8  # 실행 중 1건 외에 대기시킬 최대 건수

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            secret=clean_env_value(os.environ.get("EMBEDDER_SECRET", "")),
            queue_limit=int(clean_env_value(os.environ.get("EMBEDDER_QUEUE_LIMIT", "8"))),
        )

    def validate(self) -> None:
        # fail-closed: 비밀값이 없거나 짧으면 기동하지 않는다. 값 자체는 메시지에 넣지 않는다.
        if len(self.secret) < MIN_SECRET_LENGTH:
            raise RuntimeError(
                f"EMBEDDER_SECRET must be set and at least {MIN_SECRET_LENGTH} characters long"
            )
