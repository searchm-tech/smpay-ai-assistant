# 인수인계 문서 (세션이 끊겨도 이어받기 위한 현재 상태)

> 마지막 갱신: 2026-10-16 (A6 구현 완료)

## 한 줄 요약
SM Pay 내부 Notion 문서를 KURE-v1로 임베딩해 Supabase pgvector에 저장하고, 질문에 근거+출처로 답하는 챗 서버를 만든다.
자세한 결정과 이유는 `docs/design.md`.

## 작업 방식 (사용자 지정)
- 코드 전에 **질문 → 설계안 → 승인 → 구현**. 기획·아키텍처·보안 판단은 대안을 제시하고 사용자가 고른다.
- 한 번에 전부 만들지 않고 **한 단계씩**. 단계마다 테스트(실패 케이스 먼저)와 결과 요약.
- **커밋·푸시는 사용자가 요청할 때만.** 모르는 것은 추측하지 말고 질문.
- 로그에 질문 내용·문서 본문을 남기지 않는다.

## 진행 상황
- [x] 0단계: 질문 6개 정리, 설계 문서 초안
- [x] A0: 레포 연결(`searchm-tech/smpay-ai-assistant`), 폴더 구조, `.gitignore`, README/설계/인수인계 문서 (**미커밋**)
- [x] A1: Python 3.12 + venv + 의존성 고정 완료 (**미커밋**). brew는 Intel Mac 미지원+CLT 버전 문제로 불가 → **uv** 사용
  - 고정 조합: torch 2.2.2 / transformers 4.49.0 / sentence-transformers 3.4.1 / numpy 1.26.4 (`embedder/uv.lock`)
  - transformers 5.x는 torch 2.2.2를 인식 못 해 import 실패했음 → 4.x 계열로 제한
  - 확인: import 성공, 작은 XLM-R 랜덤 모델 forward 성공. **KURE-v1 실제 로딩은 A2에서 검증**
- [x] A2: KURE-v1 다운로드 + 스모크 테스트 통과 (**미커밋**)
  - 모델: `nlpai-lab/KURE-v1` 커밋 `8b418a58…` 고정, safetensors만, 약 2.1GB → `embedder/.cache/huggingface/` (git 제외)
  - 받기: `cd embedder && .venv/bin/python scripts/download_model.py` / 검증: `HF_HUB_OFFLINE=1 .venv/bin/python scripts/smoke_test.py`
  - 결과(맥북 Intel CPU): 차원 1024, 관련 0.817 vs 무관 0.333, 배치 일관성 cos=1.0, 질문 1건 약 430ms,
    500자 청크 약 1.5초/건, 메모리 약 2.3GB, 기본 max_seq_length=8192
  - Windows 데스크탑에서는 같은 스크립트로 다시 받고 같은 테스트를 돌려 수치를 비교할 것 (B1)
- [x] A3: 임베딩 서비스 구현 완료 (브랜치 `feature/a3-embedder-service`, **미커밋**)
  - 설계 근거: `docs/design.md`의 "A3 임베딩 서비스 설계". 코드: `embedder/app/{main,settings,schemas,service}.py`
  - 실행: `cd embedder && EMBEDDER_SECRET=<32자 이상> .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001`
    (`--env-file .env` 사용 가능, 예시는 `embedder/.env.example`). 워커는 1개로 둘 것(모델 2.3GB 복제 방지)
  - 테스트: `.venv/bin/python -m pytest -q` (27개, 모델 불필요) / 실제 모델: `RUN_SLOW=1 .venv/bin/python -m pytest tests/test_service_real.py`
  - 검증: 변형 테스트 7종(인증 우회·로그에 본문·422에 입력값·자리 미반납·대기열 제거·truncated·500 메시지)을 모두 테스트가 잡아냄
  - 실서버 확인: 질문 1건 약 0.23초(스모크 때 0.43초보다 빠름, 서버 측 측정), 127.0.0.1만 바인딩, `/docs` 비활성, 로그에 본문 없음
- [~] A4: Supabase 스키마 — **구현·로컬 검증 완료, 실제 Supabase 적용 대기** (브랜치 `feature/a4-supabase-schema`, **미커밋**)
  - 파일: `sql/001_init.sql`(테이블·함수·역할), `sql/apply_migrations.py` + `sql/dbtools/`(적용기), `sql/tests/`
  - 검증: DB 없이 `cd sql && .venv/bin/python -m pytest -q` (42개) / 로컬 DB 포함 시 62개 통과(+perf, e2e는 환경변수로)
  - 변형 테스트: SQL 정적 12종 + DB 레벨 12종 전부 탐지(처음엔 임베딩 차원 변형을 놓쳐서 테스트를 보강함)
  - 실측: 2만 청크 정확 검색 약 220ms(로컬) → 인덱스 필요 여부는 A5의 실제 청크 수로 결정 (`docs/design.md` A4 절)
  - **남은 일**: ① 사용자가 Supabase 프로젝트 생성 + `.env` 설정 ② `cd sql && .venv/bin/python apply_migrations.py` ③ 같은 테스트를 실제 Supabase에 실행해 풀러 연결(R8)·지연 확인
- [ ] A5 ~ A9, Phase B: `docs/design.md` 5절 참고

## 로컬 검증용 일회용 DB (Supabase 없이 SQL·테스트를 확인할 때)
```bash
# 1) 컨테이너 (127.0.0.1에만 바인딩, 비밀번호는 직접 생성한 임의의 값)
docker run -d --name smpay-pgvector-test -e POSTGRES_PASSWORD=<임의값> -p 127.0.0.1:54329:5432 pgvector/pgvector:pg17
# 2) Supabase 기본 환경 흉내 (anon/authenticated 에 기본 권한이 자동 부여되는 점까지 포함해야 권한 회수 테스트가 의미 있음)
docker exec -i smpay-pgvector-test psql -U postgres -v ON_ERROR_STOP=1 <<'SQL'
create role anon nologin; create role authenticated nologin; create role service_role nologin;
create schema extensions;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
grant usage on schema extensions to anon, authenticated, service_role;
SQL
# 3) .env(또는 환경변수): DATABASE_ADMIN_URL=postgresql://postgres:<임의값>@127.0.0.1:54329/postgres,
#    CHAT_READER_PASSWORD / INGEST_WRITER_PASSWORD (각 24자 이상 ASCII). 이후 apply_migrations.py → pytest
# 정리: docker rm -f smpay-pgvector-test
```
주의: 역할(`chat_reader` 등)의 설정은 **클러스터 전체**에 저장되므로 같은 컨테이너의 다른 DB에서 `reset all`을 하면 이미 적용된 설정이 사라진다.

## 환경 메모
- 현재 개발 머신: 맥북 **Intel(x86_64)**, macOS 15, RAM 16GB, 시스템 Python 3.9.6 (건드리지 않음)
- Python/가상환경은 **uv**로 관리: `pip install --user uv`(0.13.1) → `~/Library/Python/3.9/bin/uv` (PATH에 없음, 아래처럼 지정)
  - 설치: `export PATH="$HOME/Library/Python/3.9/bin:$PATH"` 후 `cd embedder && uv sync`
  - 실행: `embedder/.venv/bin/python ...` 또는 `uv run ...`
  - Python 3.12.15는 `~/.local/share/uv/python/` 에 독립 설치됨 (시스템 영향 없음)
- 로컬 경로: `/Users/seodongchan/Desktop/searchM/smpay-ai-assistant`
- git 커밋 작성자: `dongchan.seo@searchm.co.kr` (회사 계정, 전역 설정)
- 이전 구현(참고용, 수정 금지): `/Users/seodongchan/Desktop/searchM/smpay-ai-conversation-pipeline`
- 최종 배포 위치: 임베딩은 회사 Windows 데스크탑, 챗 서버는 Vercel

## A4 시작 전 사용자가 해야 할 일 (계정 로그인이 필요해 AI가 대신 못 함)
1. Supabase에서 **새 프로젝트** 생성 (권장: 회사 조직, 서울 리전, 이름 `smpay-ai-assistant`)
2. 대시보드 → Database → **Extensions**에서 `vector` 활성화 (또는 마이그레이션이 `create extension`으로 처리)
3. 연결 문자열을 **채팅에 붙이지 말고** `embedder`가 아닌 레포 루트 쪽 `.env`(커밋 금지)에 직접 저장
   - 마이그레이션용 관리자 연결(세션 풀러), 그리고 `CHAT_READER_PASSWORD`/`INGEST_WRITER_PASSWORD`로 쓸 임의의 긴 값 2개

## A5 진행 상태
- [x] **구현 완료, 가짜 응답으로 검증** (브랜치 `feature/a5-notion-ingest`, **미커밋**). 설계: `docs/design.md` "A5 Notion 수집 설계" (구현 중 **정정**: markdown을 매번 전부 조회 — 이유는 그 절 참고)
- 코드: `ingest/notion_ingest/`. 테스트: `cd ingest && .venv/bin/python -m pytest -q` (119개, 네트워크·토큰 불필요). 변형 테스트 28종 전부 탐지
- 실행(측정, DB 쓰기 없음): `cd ingest && .venv/bin/python -m notion_ingest measure [--json] [--max-pages N] [--no-titles]`
  → 설정은 레포 루트 `.env`의 `NOTION_TOKEN`, `NOTION_ROOT_PAGE_IDS` (`.env.example` 참고). 설정이 없으면 시작을 거부한다
- **남은 일**: ① 사용자가 Notion 통합(Read content만)·토큰·루트 페이지 준비 ② `measure`를 실제로 실행해 **실제 마크다운 형식 대조(R11)**, 문서 수·길이·민감정보 적중 확인 ③ 결과로 A6의 청크 크기와 A4 보류 항목(인덱스) 결정
- 토큰이 필요한 이유: 지금까지 모든 검증은 공식 문서 기반으로 추정한 형식의 가짜 응답이다. **실제 응답과 다르면 정제기가 틀릴 수 있다**

## A3-Docker: 임베딩 서비스 컨테이너 (브랜치 `feature/a3-docker`, a5 위에서 분기, **미커밋**)
- 설계·검증: `docs/design.md`의 "A3-Docker". 이미지/컨테이너 이름 `smpay-kure-model`
- 빌드·실행: `cd embedder && docker build --platform linux/amd64 -t smpay-kure-model . && scripts/docker_run.sh`
  (`embedder/.env`의 `EMBEDDER_SECRET` 필요, 모델 캐시 `embedder/.cache/huggingface`를 읽기 전용 마운트, `127.0.0.1:8001`)
- 검증: `cd embedder && .venv/bin/python scripts/docker_smoke.py` (컨테이너가 떠 있어야 함) / 정적: `pytest tests/test_docker_static.py`
- 중지·삭제: `docker rm -f smpay-kure-model`
- 주의: 이 변경으로 `embedder/pyproject.toml`·`uv.lock`이 수정됨(linux 추가). 맥북·Windows 쪽 패키지 버전은 변하지 않음(diff 확인)

## A6: 청크·임베딩·적재·동기화 (브랜치 `feature/a6-sync`, **미커밋**)
- 설계·검증·한계: `docs/design.md`의 "A6". 코드: `ingest/notion_ingest/{chunker,embed_client,store,sync,sync_report,timeutil}.py`, `sql/002_sync.sql`
- 실행: `cd ingest && .venv/bin/python -m notion_ingest sync --dry-run` (계획만) → `sync` (실제). 옵션 `--limit N` `--force-reindex` `--force-delete` `--json`
  - 설정(레포 루트 `.env`, `.env.example` 참고): `NOTION_TOKEN`, `NOTION_ROOT_PAGE_IDS`, `EMBEDDER_SECRET`(+`EMBEDDER_URL`), `INGEST_DATABASE_URL`(또는 개발용 `DATABASE_ADMIN_URL`+`INGEST_WRITER_PASSWORD`), 선택 `INGEST_ALLOW_SENSITIVE_PAGE_IDS`
  - **적용 순서**: ① `cd sql && .venv/bin/python apply_migrations.py` (002 포함, 코드보다 DB 먼저) ② 임베딩 서버 컨테이너를 **새 이미지로 재빌드**(`token_counts` 추가): `cd embedder && docker build --platform linux/amd64 -t smpay-kure-model . && scripts/docker_run.sh` ③ `sync`
- 테스트: `cd ingest && .venv/bin/python -m pytest -q` (DB·임베딩 서버 없이 248개). DB·e2e까지는 로컬 일회용 DB(위 레시피) + `EMBEDDER_URL`/`EMBEDDER_SECRET` + `CHAT_READER_PASSWORD` 환경변수로 264개. e2e는 로컬 호스트의 **빈 DB**에서만 실행됨
- 이번에 바뀐 기존 동작: ① `replace_document` 7인자 삭제 → 8인자(서명) ② embedder `/embed` 응답에 `token_counts` ③ 제목 변경은 `changed` ④ `ingest`에 `psycopg[binary]` 의존성 추가(`uv.lock` 갱신, 기존 버전 변화 없음)
- **남은 일**: ① 실제 Supabase 적용 후 `sync --dry-run` → 예상 청크 수 확인 → `sync` (R8·R11·R13 확인) ② `measure`·dry-run 결과로 청크 크기 확정 ③ 실패 알림(B4) ④ 브랜치 정리: 현재 `feature/a6-sync`에 **A3-Docker 변경(Dockerfile 등)과 A6 변경이 함께 미커밋**으로 있다. 커밋할 때 둘을 나눠서 커밋할 것(a3-docker 분 → a6 분)

## 사용자 확인 대기 중
- R1: 수집 대상을 Notion 루트 페이지 **허용 목록**으로 제한하는 것에 동의하는지
