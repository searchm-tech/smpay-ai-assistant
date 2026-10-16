import pytest
from psycopg.conninfo import conninfo_to_dict

from notion_ingest.config import ConfigError, SyncSettings, writer_dsn
from tests.fixtures import TOKEN, pid

SECRET = "e" * 40
ADMIN_PW = "AdminPw-must-not-leak-0123456789"
WRITER_PW = "WriterPw/with:special@chars-0123456789"


def env(**kw):
    base = {
        "NOTION_TOKEN": TOKEN, "NOTION_ROOT_PAGE_IDS": pid(1), "EMBEDDER_SECRET": SECRET,
        "INGEST_DATABASE_URL": "postgresql://ingest_writer:pw@db.example:5432/postgres",
    }
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


# ---------- writer_dsn ----------

def test_a_direct_url_is_used_as_is():
    url = "postgresql://ingest_writer:pw@db.example:5432/postgres"
    assert writer_dsn({"INGEST_DATABASE_URL": f'  "{url}" '}) == url


def test_the_dsn_can_be_derived_from_the_admin_url_without_keeping_the_admin_password():
    dsn = writer_dsn({
        "DATABASE_ADMIN_URL": f"postgresql://postgres:{ADMIN_PW}@db.example:5432/postgres", "INGEST_WRITER_PASSWORD": WRITER_PW,
    })
    info = conninfo_to_dict(dsn)
    assert (info["user"], info["password"], info["host"], info["dbname"]) == ("ingest_writer", WRITER_PW, "db.example", "postgres")
    assert ADMIN_PW not in dsn


def test_the_supabase_pooler_user_suffix_is_carried_over():
    dsn = writer_dsn({
        "DATABASE_ADMIN_URL": f"postgresql://postgres.abcdefghij:{ADMIN_PW}@pooler.example:5432/postgres",
        "INGEST_WRITER_PASSWORD": WRITER_PW,
    })
    assert conninfo_to_dict(dsn)["user"] == "ingest_writer.abcdefghij"


def test_missing_database_settings_are_rejected_without_echoing_values():
    for environ in ({}, {"DATABASE_ADMIN_URL": f"postgresql://postgres:{ADMIN_PW}@h/db"}, {"INGEST_WRITER_PASSWORD": WRITER_PW}):
        with pytest.raises(ConfigError) as e:
            writer_dsn(environ)
        assert ADMIN_PW not in str(e.value) and WRITER_PW not in str(e.value) and "INGEST_DATABASE_URL" in str(e.value)


# ---------- SyncSettings ----------

def test_sync_settings_defaults_and_parsing():
    s = SyncSettings.from_env(env(INGEST_ALLOW_SENSITIVE_PAGE_IDS=f"{pid(2)}, {pid(3)}"))
    assert s.embedder_url == "http://127.0.0.1:8001" and s.embedder_secret == SECRET
    assert s.allow_sensitive == frozenset({pid(2), pid(3)})
    assert s.notion.root_page_ids == (pid(1),) and s.database_url.startswith("postgresql://")


def test_the_allow_list_is_optional_and_empty_by_default():
    assert SyncSettings.from_env(env()).allow_sensitive == frozenset()


@pytest.mark.parametrize("override", [{"EMBEDDER_SECRET": None}, {"EMBEDDER_SECRET": "short"}, {"EMBEDDER_URL": "ftp://x"}, {"EMBEDDER_URL": "not a url"}])
def test_invalid_embedder_settings_are_rejected(override):
    with pytest.raises(ConfigError) as e:
        SyncSettings.from_env(env(**override))
    assert "short" not in str(e.value) and "EMBEDDER_" in str(e.value)


def test_an_invalid_allow_list_entry_is_rejected_without_echoing_it():
    with pytest.raises(ConfigError) as e:
        SyncSettings.from_env(env(INGEST_ALLOW_SENSITIVE_PAGE_IDS="definitely-not-an-id"))
    assert "definitely-not-an-id" not in str(e.value) and "INGEST_ALLOW_SENSITIVE_PAGE_IDS" in str(e.value)


def test_secrets_never_appear_in_repr():
    s = SyncSettings.from_env(env())
    assert SECRET not in repr(s) and TOKEN not in repr(s) and "pw@" not in repr(s)
