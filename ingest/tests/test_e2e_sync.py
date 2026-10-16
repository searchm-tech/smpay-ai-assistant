"""실제 임베딩 서버(KURE-v1 컨테이너) + 실제 Postgres + 가짜 Notion 으로 `sync` 전체를 검증한다.

필요: 실행 중인 임베딩 서버, EMBEDDER_URL / EMBEDDER_SECRET, 로컬 DB(DATABASE_ADMIN_URL·INGEST_WRITER_PASSWORD·CHAT_READER_PASSWORD).
'없는 문서를 지우는' 동기화를 실제로 실행하므로 로컬 호스트의 빈 DB 에서만 실행된다(conftest.local_empty_db).
"""

import os

import pytest
from psycopg import connect
from psycopg.conninfo import make_conninfo

from notion_ingest.cli import main
from notion_ingest.embed_client import EmbedClient
from tests.conftest import FakeNotion, child_tag
from tests.fixtures import pid
from tests.test_sync import CARD

pytestmark = [pytest.mark.db, pytest.mark.e2e]

CHARGE = (
    "광고비 충전 한도 변경 방법과 신청 절차를 안내합니다. 충전 한도 증액은 관리자 승인 후 반영되며 영업일 기준 1일 이내 처리됩니다. "
    "한도를 늘리려면 신청서를 작성하고 승인을 기다려 주세요."
)
SETTLE = (
    "정산은 매월 말일에 진행됩니다. 정산 금액은 수수료를 제외한 광고 집행 금액이며 다음 달 5영업일 이내에 입금됩니다. "
    "정산 내역은 관리자 화면에서 확인할 수 있습니다."
)
LUNCH = "이번 주 사무실 점심 메뉴로 추천하는 맛집 목록입니다. 김치찌개, 제육볶음, 칼국수 등 다양한 메뉴가 있습니다. 투표로 정해요."


def tree(*, settle_extra="", with_lunch=True, extra_docs=()):
    notion = FakeNotion()
    tags = [child_tag(2, "충전 한도 안내"), child_tag(3, "정산 안내")] + ([child_tag(4, "점심 메뉴")] if with_lunch else [])
    tags += [child_tag(n, title) for n, title, _ in extra_docs]
    notion.add(1, "SM Pay 가이드", "# SM Pay 가이드\n\nSM Pay 서비스 운영 문서 모음입니다. 아래 하위 문서를 참고하세요.\n\n" + "\n".join(tags))
    notion.add(2, "충전 한도 안내", f"# 충전 한도 안내\n\n{CHARGE}", parent=1)
    notion.add(3, "정산 안내", f"# 정산 안내\n\n{SETTLE}{settle_extra}", parent=1)
    if with_lunch:
        notion.add(4, "점심 메뉴", f"# 점심 메뉴\n\n{LUNCH}", parent=1)
    for n, title, text in extra_docs:
        notion.add(n, title, f"# {title}\n\n{text}", parent=1)
    return notion


@pytest.fixture
def env(db_urls):
    url, secret = os.environ.get("EMBEDDER_URL"), os.environ.get("EMBEDDER_SECRET")
    if not url or not secret:
        pytest.skip("EMBEDDER_URL / EMBEDDER_SECRET 이 없어 건너뜀")
    return {
        "NOTION_TOKEN": "ntn_e2e_fake_token_" + "x" * 20, "NOTION_ROOT_PAGE_IDS": pid(1),
        "EMBEDDER_URL": url, "EMBEDDER_SECRET": secret, "INGEST_DATABASE_URL": db_urls["writer"],
    }


def cli_sync(notion, env, *args):
    out, err = [], []
    code = main(["sync", *args], environ=env, client_factory=lambda settings: notion, out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)  # 임베딩 서버·DB 는 실제 기본 팩토리로 연결된다


def chunk_ids(admin, title):
    return [r[0] for r in admin.execute(
        "select c.id from public.chunks c join public.documents d on d.id = c.document_id where d.title = %s order by c.chunk_index",
        (title,),
    ).fetchall()]


def test_full_sync_search_update_delete_and_quarantine(env, local_empty_db):
    admin = local_empty_db

    # 1) 첫 동기화: 실제 임베딩 서버로 모두 색인
    code, out, err = cli_sync(tree(), env)
    assert code == 0, out + err
    assert admin.execute("select count(*) from public.documents").fetchone()[0] == 4
    rows = admin.execute(
        "select count(*), min(token_count), count(distinct embedding_revision), min(embedding_model), "
        "count(*) filter (where truncated), count(*) filter (where extensions.vector_dims(embedding) <> 1024) from public.chunks"
    ).fetchone()
    assert rows[0] >= 4 and rows[1] >= 1 and rows[2] == 1 and rows[3] == "nlpai-lab/KURE-v1" and rows[4] == 0 and rows[5] == 0
    assert admin.execute("select count(*) from public.documents where index_signature is null").fetchone()[0] == 0
    assert admin.execute("select count(*) from public.chunks where heading_path not like '%' || (select title from public.documents d where d.id = document_id) || '%'").fetchone()[0] == 0
    assert admin.execute("select status, new_count from public.sync_runs").fetchall() == [("complete", 4)]

    # 2) 검색: 질문을 같은 서버로 임베딩해 chat_reader 로 검색하면 관련 문서가 맨 위
    client = EmbedClient(env["EMBEDDER_URL"], env["EMBEDDER_SECRET"])
    try:
        batch = client.embed(["광고비 충전 한도를 늘리려면 어떻게 해야 하나요?"])
    finally:
        client.close()
    reader = make_conninfo(env["INGEST_DATABASE_URL"], user="chat_reader", password=os.environ["CHAT_READER_PASSWORD"])
    with connect(reader, autocommit=True) as chat:
        vec = "[" + ",".join(f"{x:.6f}" for x in batch.vectors[0]) + "]"
        found = chat.execute(
            "select title, similarity from public.match_chunks(%s::extensions.vector, %s, 5)", (vec, batch.revision)
        ).fetchall()
    assert found[0][0] == "충전 한도 안내" and found[0][1] > found[-1][1]

    # 3) 변경 없음: 아무것도 다시 쓰지 않는다(청크 id 가 그대로)
    before = {t: chunk_ids(admin, t) for t in ("충전 한도 안내", "정산 안내", "점심 메뉴")}
    code, out, _ = cli_sync(tree(), env)
    assert code == 0 and "변경 없음 4" in out and "신규 0" in out
    assert before == {t: chunk_ids(admin, t) for t in before}

    # 4) 한 문서만 변경: 그 문서의 청크만 교체
    code, out, _ = cli_sync(tree(settle_extra=" 정산 지연 시 고객센터로 문의해 주세요."), env)
    after = {t: chunk_ids(admin, t) for t in before}
    assert code == 0 and "변경 1" in out
    assert after["충전 한도 안내"] == before["충전 한도 안내"] and after["점심 메뉴"] == before["점심 메뉴"]
    assert after["정산 안내"] != before["정산 안내"]
    assert admin.execute("select count(*) from public.chunks where content like '%고객센터%'").fetchone()[0] >= 1

    # 5) 문서 삭제: Notion 에서 사라진 문서는 청크까지 지워진다
    code, out, _ = cli_sync(tree(settle_extra=" 정산 지연 시 고객센터로 문의해 주세요.", with_lunch=False), env)
    assert code == 0 and "삭제 1" in out
    assert admin.execute("select count(*) from public.documents where title = '점심 메뉴'").fetchone()[0] == 0
    assert chunk_ids(admin, "점심 메뉴") == []

    # 6) 민감정보 문서는 색인되지 않는다
    code, out, _ = cli_sync(
        tree(settle_extra=" 정산 지연 시 고객센터로 문의해 주세요.", with_lunch=False, extra_docs=[(5, "테스트 카드", CARD + " " + LUNCH)]),
        env,
    )
    assert code == 0 and "격리 1" in out and pid(5) in out
    assert admin.execute("select count(*) from public.documents where title = '테스트 카드'").fetchone()[0] == 0
    assert admin.execute("select count(*) from public.chunks where content like '%4111%'").fetchone()[0] == 0

    # 7) 실행 기록: 개수와 상태만
    statuses = admin.execute("select status from public.sync_runs order by started_at").fetchall()
    assert [s[0] for s in statuses] == ["complete"] * 5  # 1·3·4·5·6단계에서 5번 실행
    assert admin.execute("select count(*) from public.sync_runs where finished_at is null").fetchone()[0] == 0


def test_dry_run_against_the_real_stack_writes_nothing(env, local_empty_db):
    code, out, _ = cli_sync(tree(), env, "--dry-run")
    assert code == 0 and "dry-run" in out and "신규 4" in out and "예상 청크" in out
    assert local_empty_db.execute("select count(*) from public.documents").fetchone()[0] == 0
    assert local_empty_db.execute("select count(*) from public.sync_runs").fetchone()[0] == 0


def test_a_wrong_embedder_secret_stops_before_any_database_write(env, local_empty_db):
    code, out, _ = cli_sync(tree(), {**env, "EMBEDDER_SECRET": "w" * 40})
    assert code == 1 and "embedder_auth" in out
    assert local_empty_db.execute("select count(*) from public.documents").fetchone()[0] == 0


def test_an_unreachable_embedder_stops_before_any_database_write(env, local_empty_db):
    code, out, _ = cli_sync(tree(), {**env, "EMBEDDER_URL": "http://127.0.0.1:9"})
    assert code == 1 and "embedder_unavailable" in out
    assert local_empty_db.execute("select count(*) from public.documents").fetchone()[0] == 0
