from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.settings import MAX_CHARS, MAX_TEXTS


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texts: list[Annotated[str, StringConstraints(max_length=MAX_CHARS)]] = Field(
        min_length=1, max_length=MAX_TEXTS
    )


class EmbedResponse(BaseModel):
    model: str
    revision: str
    dim: int
    embeddings: list[list[float]]
    truncated: list[bool]
    token_counts: list[int]  # 입력별 실제 토큰 수(자르기 전). 512 초과면 truncated
