"""PostgreSQL SCRAM-SHA-256 비밀번호 검증값(verifier) 생성.

`ALTER ROLE ... PASSWORD '평문'`은 평문이 서버 로그·통계에 남을 수 있다. psql의 \\password처럼
클라이언트에서 검증값을 만들어 보내면 평문은 네트워크를 지나지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

ITERATIONS = 4096


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    if not password.isascii():
        # PostgreSQL은 SASLprep을 적용한다. 정규화 차이를 피하려고 ASCII 비밀번호만 허용한다.
        raise ValueError("password must be ASCII only")
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return f"SCRAM-SHA-256${iterations}:{_b64(salt)}${_b64(stored_key)}:{_b64(server_key)}"
