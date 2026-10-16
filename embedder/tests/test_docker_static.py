"""컨테이너 구성(Dockerfile, .dockerignore, uv.lock의 linux 해석)이 보안·재현성 속성을 지키는지 Docker 없이 검사한다.
실제 컨테이너 동작은 scripts/docker_smoke.py 에서 확인한다."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _instructions(dockerfile: str) -> list[str]:
    """주석을 빼고 줄 이음(\\)을 합친 Dockerfile 명령 목록."""
    joined = re.sub(r"\\\s*\n", " ", dockerfile)
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_dockerfile_runs_as_non_root_user():
    users = [i.split(None, 1)[1] for i in _instructions(_read("Dockerfile")) if i.upper().startswith("USER ")]
    assert users, "USER 지시어가 없다"
    assert users[-1].split(":")[0] not in {"root", "0"}


def test_dockerfile_has_no_secret_values():
    text = _read("Dockerfile")
    assert not re.search(r"EMBEDDER_SECRET\s*=\s*\S", text)  # 비밀값은 실행 시 --env-file 로만
    assert not re.search(r"(ENV|ARG)\s+EMBEDDER_SECRET", text)


def test_dockerfile_forces_offline_model_loading():
    assert re.search(r"HF_HUB_OFFLINE\s*=\s*1", _read("Dockerfile"))


def test_dockerfile_copies_only_app_code_not_whole_context():
    copies = [i for i in _instructions(_read("Dockerfile")) if i.upper().startswith("COPY ")]
    assert copies
    for c in copies:
        args = [a for a in c.split()[1:] if not a.startswith("--")]
        assert args[0] not in {".", "./", "*"}, c


def test_dockerfile_installs_from_frozen_lock():
    text = _read("Dockerfile")
    assert re.search(r"uv sync[^\n]*--frozen", text)
    assert "--no-dev" in text


def test_dockerfile_runs_single_worker_without_reload():
    cmd = [i for i in _instructions(_read("Dockerfile")) if i.upper().startswith(("CMD ", "ENTRYPOINT "))]
    assert cmd and "uvicorn" in cmd[-1] and "app.main:app" in cmd[-1]
    assert "--reload" not in cmd[-1]
    assert "--workers" not in cmd[-1]  # 모델(2.3GB) 복제 방지: 워커는 1개


def test_dockerignore_keeps_secrets_venv_and_model_out_of_build_context():
    lines = {line.strip().rstrip("/") for line in _read(".dockerignore").splitlines()}
    for must in (".env", ".venv", ".cache", ".git", "tests"):
        assert must in lines, must
    assert ".env.*" in lines  # .env.example 포함 모든 변형 제외(이미지에 환경 파일이 필요 없다)


def test_uv_lock_covers_linux_x86_64_with_cpu_only_torch():
    pyproject = tomllib.loads(_read("pyproject.toml"))
    envs = " ".join(pyproject["tool"]["uv"]["environments"])
    assert "linux" in envs and "x86_64" in envs

    index_urls = [i["url"] for i in pyproject["tool"]["uv"]["index"]]
    assert "https://download.pytorch.org/whl/cpu" in index_urls

    lock = tomllib.loads(_read("uv.lock"))
    torch_versions = {p["version"] for p in lock["package"] if p["name"] == "torch"}
    # 모든 플랫폼에서 같은 torch 2.2.2 계열이어야 한다(CUDA 번들 휠이 섞이면 이미지가 수 GB가 됨)
    assert torch_versions and all(v.split("+")[0] == "2.2.2" for v in torch_versions), torch_versions
    assert "2.2.2+cpu" in torch_versions
    nvidia = [p["name"] for p in lock["package"] if p["name"].startswith("nvidia-")]
    assert nvidia == [], nvidia  # CPU 전용: CUDA 라이브러리가 lock에 없어야 함
