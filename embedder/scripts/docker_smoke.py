"""smpay-kure-model 컨테이너 검증. 컨테이너가 떠 있는 상태에서 호스트의 embedder venv로 실행한다.

실행: cd embedder && .venv/bin/python scripts/docker_smoke.py
검증: 인증 / 벡터 차원·revision / 네이티브 모델과의 수치 일치 / 포트 노출 범위 / 로그에 본문 없음 /
      컨테이너 보안 설정 / 응답 시간·메모리. 비밀값은 .env 에서 읽고 출력하지 않는다.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")  # docker 호출(fork) 시 경고 방지
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from kure_config import EMBEDDING_DIM, REVISION  # noqa: E402

NAME = "smpay-kure-model"
URL = "http://127.0.0.1:8001"
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def secret() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("EMBEDDER_SECRET="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("embedder/.env 에 EMBEDDER_SECRET 이 없습니다")


def post(texts: list[str], key: str | None):
    headers = {"content-type": "application/json"}
    if key is not None:
        headers["x-internal-secret"] = key
    req = urllib.request.Request(URL + "/embed", json.dumps({"texts": texts}).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False).stdout.strip()


def main() -> int:
    key = secret()

    # 1) 인증
    check("시크릿 없는 요청 -> 401", post(["테스트"], None)[0] == 401)
    check("잘못된 시크릿 -> 401", post(["테스트"], "x" * 64)[0] == 401)

    # 2) 벡터
    marker = f"로그노출확인-{uuid.uuid4().hex}"
    texts = ["SM Pay 정산 주기는 어떻게 되나요?", "정산은 매월 말일에 진행됩니다.", "오늘 점심 메뉴 추천해줘", marker]
    status, body = post(texts, key)
    ok = status == 200 and body is not None
    check("올바른 시크릿 -> 200", ok, f"status={status}")
    if not ok:
        return 1
    vectors = np.array(body["embeddings"])
    check("벡터 차원 1024, 입력 수와 일치", vectors.shape == (len(texts), EMBEDDING_DIM), str(vectors.shape))
    check("모델 revision 고정값", body["revision"] == REVISION, body["revision"][:8])
    check("truncated 없음", body["truncated"] == [False] * len(texts))
    check("token_counts 가 입력 수와 같고 양수", len(body["token_counts"]) == len(texts) and all(n >= 1 for n in body["token_counts"]), str(body["token_counts"]))
    sims = vectors @ vectors.T
    check("관련 문장 > 무관 문장 유사도", sims[0, 1] > sims[0, 2], f"{sims[0, 1]:.3f} vs {sims[0, 2]:.3f}")

    # 3) 네이티브(맥북) 모델과 수치 일치
    from app.service import KureEmbeddingService

    native, _, native_tokens = KureEmbeddingService.load().embed(texts)
    cos = (vectors * native).sum(axis=1)  # 둘 다 정규화되어 있어 내적 = 코사인
    check("컨테이너 vs 네이티브 cos >= 0.999", cos.min() >= 0.999, f"min={cos.min():.6f}")
    check("컨테이너 vs 네이티브 토큰 수 일치", body["token_counts"] == [int(n) for n in native_tokens])

    # 4) 포트 노출 범위: 호스트의 LAN IP로는 접속이 거부되어야 한다
    ports = docker("port", NAME)
    check("publish 는 127.0.0.1 에만", ports != "" and all(l.split("-> ")[1].startswith("127.0.0.1:") for l in ports.splitlines()), ports.replace("\n", " | "))
    lan = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True).stdout.strip()
    if lan:
        s = socket.socket()
        s.settimeout(3)
        refused = s.connect_ex((lan, 8001)) != 0
        s.close()
        check("LAN IP 로는 접속 거부", refused, f"(en0 주소로 시도)")
    else:
        print("[SKIP] LAN IP 를 찾지 못해 접속 거부 확인을 건너뜀")

    # 5) 로그에 본문이 없는지
    logs = docker("logs", NAME)
    # docker logs 는 stderr 로도 나오므로 합쳐서 본다
    logs += subprocess.run(["docker", "logs", NAME], capture_output=True, text=True).stderr
    check("로그에 요청 본문(마커) 없음", marker not in logs and "정산은 매월" not in logs)
    check("로그에 시크릿 없음", key not in logs)

    # 6) 컨테이너 보안 설정
    cfg = json.loads(docker("inspect", NAME))[0]
    host = cfg["HostConfig"]
    check("비루트 사용자", cfg["Config"]["User"] not in ("", "0", "root"), cfg["Config"]["User"])
    check("루트 파일시스템 읽기 전용", host["ReadonlyRootfs"] is True)
    check("cap-drop ALL", host["CapDrop"] == ["ALL"])
    check("no-new-privileges", "no-new-privileges" in " ".join(host["SecurityOpt"] or []))
    mounts = {m["Destination"]: m["RW"] for m in cfg["Mounts"]}
    check("모델 캐시는 읽기 전용 마운트", mounts.get("/app/.cache/huggingface") is False, str(mounts))
    check("이미지 환경변수에 시크릿 값 없음", key not in " ".join(json.loads(docker("image", "inspect", NAME))[0]["Config"]["Env"]))

    # 7) 응답 시간(워밍업 후 질문 1건 x5) / 메모리
    times = []
    for _ in range(5):
        t = time.perf_counter()
        post(["SM Pay 정산 주기는 어떻게 되나요?"], key)
        times.append(time.perf_counter() - t)
    print(f"[INFO] 질문 1건 응답시간 중앙값 {sorted(times)[2] * 1000:.0f}ms (네이티브 서버 측정 약 230ms)")
    print(f"[INFO] 컨테이너 메모리: {docker('stats', '--no-stream', '--format', '{{.MemUsage}}', NAME)}")

    print(f"\n실패 {failures}건")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
