import json

from notion_ingest.cli import main
from tests.conftest import child_tag
from tests.fixtures import SENTINEL, TOKEN, pid


def env(**kw):
    base = {"NOTION_TOKEN": TOKEN, "NOTION_ROOT_PAGE_IDS": pid(1)}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def run(argv, environ, fake=None):
    out, err = [], []
    code = main(argv, environ=environ, client_factory=(lambda settings: fake), out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


def populated(fake):
    fake.add(1, "루트", f"# 루트\n{SENTINEL}\n" + child_tag(2, "A"))
    fake.add(2, "A", "# A", parent=1)
    return fake


def test_measure_prints_a_readable_report_without_content(fake):
    code, out, _ = run(["measure"], env(), populated(fake))
    assert code == 0 and "문서" in out and SENTINEL not in out and TOKEN not in out


def test_measure_json_is_valid_and_content_free(fake):
    code, out, _ = run(["measure", "--json"], env(), populated(fake))
    data = json.loads(out)
    assert code == 0 and data["documents"] == 2 and SENTINEL not in out


def test_missing_token_or_roots_exit_with_usage_error_and_leak_nothing(fake):
    for e in (env(NOTION_TOKEN=None), env(NOTION_ROOT_PAGE_IDS=None)):
        code, out, err = run(["measure"], e, fake)
        assert code == 2 and "NOTION_" in err and TOKEN not in out + err


def test_auth_failure_exits_with_a_generic_message(fake):
    from notion_ingest.notion_client import NotionAuthError
    fake.page_errors[pid(1)] = NotionAuthError("unauthorized", 401)
    code, out, err = run(["measure"], env(), fake)
    assert code == 1 and "401" in err and TOKEN not in out + err


def test_max_pages_option_is_applied(fake):
    code, out, _ = run(["measure", "--json", "--max-pages", "1"], env(), populated(fake))
    data = json.loads(out)
    assert data["documents"] == 1 and data["complete"] is False


def test_unknown_command_is_a_usage_error(fake):
    code, _, err = run(["frobnicate"], env(), fake)
    assert code == 2
