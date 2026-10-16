import pytest

from notion_ingest.config import ConfigError, Settings, parse_root_ids
from notion_ingest.ids import normalize_page_id
from tests.fixtures import TOKEN, pid

HEX = "0123456789abcdef0123456789abcdef"
DASHED = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.mark.parametrize(
    "value",
    [
        HEX,
        DASHED,
        DASHED.upper(),
        f"https://www.notion.so/{HEX}",
        f"https://www.notion.so/워크스페이스/Page-Title-{HEX}?pvs=4",
        f"https://www.notion.so/Page-{HEX}#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        f"https://www.notion.so/workspace?p={HEX}&pm=s",
    ],
)
def test_normalize_page_id_accepts_ids_and_urls(value):
    assert normalize_page_id(value) == DASHED


@pytest.mark.parametrize("value", ["", "not-an-id", "https://www.notion.so/workspace", "0123", HEX + "ff"])
def test_normalize_page_id_rejects_garbage(value):
    assert normalize_page_id(value) is None


def test_parse_root_ids_splits_dedups_and_keeps_order():
    raw = f'  "{HEX}",\n{pid(2)} {pid(3)};{HEX}  '
    assert parse_root_ids(raw) == (DASHED, pid(2), pid(3))


def test_parse_root_ids_empty_is_rejected_fail_closed():
    for raw in ("", "   ", ",,\n"):
        with pytest.raises(ConfigError):
            parse_root_ids(raw)


def test_parse_root_ids_invalid_entry_names_position_but_does_not_echo_value():
    with pytest.raises(ConfigError) as e:
        parse_root_ids(f"{HEX}, https://example.com/secret-looking-thing")
    assert "#2" in str(e.value) and "secret-looking-thing" not in str(e.value)


def env(**kw):
    base = {"NOTION_TOKEN": TOKEN, "NOTION_ROOT_PAGE_IDS": HEX}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def test_settings_defaults_and_cleaning():
    s = Settings.from_env(env(NOTION_TOKEN=f'  "{TOKEN}"  '))
    assert s.token == TOKEN and s.root_page_ids == (DASHED,)
    assert s.rate_per_sec == 2.5 and s.max_pages == 5000


def test_settings_missing_token_or_roots_refuses_to_start():
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env(NOTION_TOKEN=None))
    assert "NOTION_TOKEN" in str(e.value)
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env(NOTION_ROOT_PAGE_IDS=None))
    assert "NOTION_ROOT_PAGE_IDS" in str(e.value)


@pytest.mark.parametrize("name,value", [("NOTION_RATE_PER_SEC", "0"), ("NOTION_RATE_PER_SEC", "abc"),
                                        ("NOTION_RATE_PER_SEC", "-1"), ("NOTION_MAX_PAGES", "0"),
                                        ("NOTION_MAX_PAGES", "x")])
def test_settings_rejects_invalid_numbers(name, value):
    with pytest.raises(ConfigError):
        Settings.from_env(env(**{name: value}))


def test_settings_repr_and_errors_never_contain_the_token():
    s = Settings.from_env(env())
    assert TOKEN not in repr(s) and TOKEN not in str(s)
    with pytest.raises(ConfigError) as e:
        Settings.from_env(env(NOTION_RATE_PER_SEC="abc"))
    assert TOKEN not in str(e.value)
