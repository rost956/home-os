from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL_SCRIPTS = (
    ROOT / "scripts" / "install_llama_cpp.sh",
    ROOT / "scripts" / "download_home_ai_model.sh",
    ROOT / "scripts" / "run_llama_server.sh",
    ROOT / "scripts" / "wait_llama_server.sh",
)


def _parse_env_template(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid environment line: {raw_line}"
        assert key not in result, f"duplicate environment key: {key}"
        result[key] = value
    return result


def test_llama_environment_template_has_conservative_pi_defaults():
    values = _parse_env_template(ROOT / "ops" / "home-ai" / "llama-server.env.example")

    assert values["LLAMA_SERVER_BIN"].startswith("/opt/home-ai/")
    assert values["LLAMA_WORKING_DIRECTORY"].startswith("/opt/home-ai/")
    assert values["LLAMA_MODEL_PATH"].startswith("/opt/home-ai/models/")
    assert values["LLAMA_MODEL_PATH"].endswith(".gguf")
    assert values["LLAMA_MODEL_ALIAS"] == "qwen3.5-4b-q4_k_m"
    assert values["LLAMA_HOST"] not in {"0.0.0.0", "::", "127.0.0.1"}
    assert int(values["LLAMA_PORT"]) == 8081
    assert int(values["LLAMA_STARTUP_TIMEOUT_SECONDS"]) < 600
    assert 4096 <= int(values["LLAMA_CTX_SIZE"]) <= 8192
    assert int(values["LLAMA_BATCH_SIZE"]) <= 512
    assert int(values["LLAMA_UBATCH_SIZE"]) <= int(values["LLAMA_BATCH_SIZE"])
    assert int(values["LLAMA_PARALLEL"]) == 1
    assert values["LLAMA_API_KEY_FILE"].startswith("/etc/home-ai/")


def test_systemd_unit_is_unprivileged_bounded_and_not_public():
    unit = (ROOT / "ops" / "home-ai" / "llama-server.service").read_text(encoding="utf-8")

    assert "User=home-ai" in unit
    assert "Group=home-ai" in unit
    assert "EnvironmentFile=/etc/home-ai/llama-server.env" in unit
    assert "Restart=on-failure" in unit
    assert "TimeoutStartSec=" in unit
    assert "ExecStartPost=/opt/home-ai/bin/wait_llama_server.sh" in unit
    assert "MemoryHigh=5G" in unit
    assert "MemoryMax=6G" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "0.0.0.0" not in unit


def test_compose_maps_host_gateway_without_publishing_llama_port():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert '"host.docker.internal:host-gateway"' in compose
    assert "AI_READ_TIMEOUT_SECONDS: ${AI_READ_TIMEOUT_SECONDS:-120}" in compose
    assert "8081:8081" not in compose
    assert "llama" not in caddy.lower()


def test_home_ai_remains_disabled_in_example_and_has_unambiguous_runtime_values():
    values = _parse_env_template(ROOT / ".env.example")

    assert values["AI_ENABLED"] == "false"
    assert values["AI_CONNECT_TIMEOUT_SECONDS"] == "5"
    assert values["AI_READ_TIMEOUT_SECONDS"] == "120"
    assert values["AI_MAX_TOKENS"] == "512"
    assert values["AI_CONTEXT_BUDGET"] == "6000"
    assert values["AI_MAX_CONCURRENCY"] == "1"
    assert values["AI_ENABLE_THINKING"] == "false"


def test_runtime_scripts_do_not_embed_models_or_touch_home_os_data():
    assert not list(ROOT.rglob("*.gguf"))
    assert "*.gguf" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.gguf" in (ROOT / ".dockerignore").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install_llama_cpp.sh").read_text(encoding="utf-8")
    downloader = (ROOT / "scripts" / "download_home_ai_model.sh").read_text(encoding="utf-8")

    assert "https://github.com/ggml-org/llama.cpp.git" in installer
    assert 'LLAMA_CPP_REVISION="${LLAMA_CPP_REVISION:-v0.4.0}"' in installer
    assert "recipe_budget_service/data" not in installer
    assert "recipe_budget_service/data" not in downloader
    assert ".partial" in downloader
    assert "sha256sum" in downloader
    assert "mv --no-clobber --no-target-directory" in downloader


def test_manual_python_smoke_and_benchmark_mock_modes(tmp_path: Path):
    smoke = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "home_ai_runtime_smoke.py"), "--mock"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert "no network or model used" in smoke.stdout

    output = tmp_path / "benchmark.json"
    benchmark = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "home_ai_benchmark.py"),
            "--mock",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert benchmark.returncode == 0, benchmark.stderr
    assert output.is_file()
    assert '"case_count": 7' in output.read_text(encoding="utf-8")


def test_shell_scripts_parse_with_bash_when_available():
    if os.name == "nt":
        return
    bash = shutil.which("bash")
    if not bash:
        return
    for script in SHELL_SCRIPTS:
        result = subprocess.run([bash, "-n", str(script)], check=False, capture_output=True, text=True)
        assert result.returncode == 0, f"{script}: {result.stderr}"
