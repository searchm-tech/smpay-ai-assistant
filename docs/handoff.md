# 인수인계 문서 (세션이 끊겨도 이어받기 위한 현재 상태)

> 마지막 갱신: 2026-10-16

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
- [ ] A4 ~ A9, Phase B: `docs/design.md` 5절 참고

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

## 사용자 확인 대기 중
- R1: 수집 대상을 Notion 루트 페이지 **허용 목록**으로 제한하는 것에 동의하는지
