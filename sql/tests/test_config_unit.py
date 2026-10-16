import pytest
from psycopg.conninfo import conninfo_to_dict

from dbtools.config import (
    ConfigError,
    clean_env_value,
    require_env,
    role_dsn,
    validate_password,
)

POOLER_ADMIN = "postgresql://postgres.abcdref:AdminPw123@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
DIRECT_ADMIN = "postgresql://postgres:AdminPw123@db.abcdref.supabase.co:5432/postgres"


def test_clean_env_value_strips_whitespace_and_quotes():
    assert clean_env_value('  "abc"  ') == "abc"
    assert clean_env_value("  'abc'  ") == "abc"
    assert clean_env_value("abc") == "abc"
    assert clean_env_value('"a"b"') == 'a"b'


def test_role_dsn_for_pooler_appends_project_ref_to_role_name():
    d = conninfo_to_dict(role_dsn(POOLER_ADMIN, "chat_reader", "x" * 30))
    assert d["user"] == "chat_reader.abcdref"
    assert d["password"] == "x" * 30
    assert d["host"] == "aws-0-ap-northeast-2.pooler.supabase.com"
    assert d["port"] == "5432" and d["dbname"] == "postgres"


def test_role_dsn_for_direct_connection_uses_plain_role_name():
    d = conninfo_to_dict(role_dsn(DIRECT_ADMIN, "ingest_writer", "y" * 30))
    assert d["user"] == "ingest_writer"
    assert d["host"] == "db.abcdref.supabase.co"


def test_role_dsn_does_not_carry_over_admin_password():
    assert "AdminPw123" not in role_dsn(POOLER_ADMIN, "chat_reader", "x" * 30)


def test_validate_password_rejects_short_without_echoing_value():
    with pytest.raises(ConfigError) as e:
        validate_password("short-pw", "CHAT_READER_PASSWORD")
    assert "short-pw" not in str(e.value) and "CHAT_READER_PASSWORD" in str(e.value)


def test_validate_password_rejects_non_ascii():
    with pytest.raises(ConfigError):
        validate_password("가" * 30, "CHAT_READER_PASSWORD")


def test_validate_password_accepts_long_ascii():
    assert validate_password("a" * 24, "X") == "a" * 24


def test_require_env_missing_names_the_variable_only(monkeypatch):
    monkeypatch.delenv("DATABASE_ADMIN_URL", raising=False)
    with pytest.raises(ConfigError) as e:
        require_env("DATABASE_ADMIN_URL")
    assert "DATABASE_ADMIN_URL" in str(e.value)


def test_require_env_cleans_quotes(monkeypatch):
    monkeypatch.setenv("SOME_VAR", '  "value"  ')
    assert require_env("SOME_VAR") == "value"
