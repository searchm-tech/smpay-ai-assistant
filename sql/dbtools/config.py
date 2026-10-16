from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_PASSWORD_LENGTH = 24


class ConfigError(RuntimeError):
    """설정 오류. 메시지에는 변수 이름만 담고 값은 절대 담지 않는다."""


def clean_env_value(value: str) -> str:
    """환경변수에 섞이기 쉬운 앞뒤 공백과 감싼 따옴표를 정리한다."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def load_env() -> None:
    """레포 루트의 .env(git 제외)를 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    load_dotenv(REPO_ROOT / ".env", override=False)


def require_env(name: str) -> str:
    value = clean_env_value(os.environ.get(name, ""))
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def validate_password(value: str, name: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ConfigError(f"{name} must be at least {MIN_PASSWORD_LENGTH} characters")
    if not value.isascii():
        raise ConfigError(f"{name} must be ASCII only")
    return value


def role_dsn(admin_dsn: str, role: str, password: str) -> str:
    """관리자 연결 문자열에서 사용자·비밀번호만 바꿔 역할별 연결 문자열을 만든다.

    Supabase 풀러는 사용자명이 `역할.프로젝트ref` 형식이라 admin 사용자명의 `.ref` 부분을 이어 붙인다.
    """
    user = conninfo_to_dict(admin_dsn).get("user", "")
    project_ref = user.split(".", 1)[1] if "." in user else None
    new_user = f"{role}.{project_ref}" if project_ref else role
    return make_conninfo(admin_dsn, user=new_user, password=password)
