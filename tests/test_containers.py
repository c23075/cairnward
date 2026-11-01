"""Container templates (FR-ISO-4): the client has no route out except Cairnward's socket.

Checked with `docker compose config` (parses and merges the files; no daemon, no
images). Skipped when the docker CLI with compose isn't installed."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent / "deploy" / "containers"
ENV = {"OPEN_WEBUI_IMAGE": "ghcr.io/open-webui/open-webui:v0.0.0", "SOCAT_IMAGE": "alpine/socat:1",
       "CAIRNWARD_KEY": "cw-test", "CAIRNWARD_CLIENT_SOCKETS": "/state/clients"}  # fmt: skip


def compose(*files: Path, env: dict[str, str]) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None or sys.platform == "win32":
        pytest.skip("docker CLI not installed")
    args = [docker, "compose"] + [a for f in files for a in ("-f", str(f))]
    res = subprocess.run([*args, "config", "--format", "json"], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", **env}, check=False)  # fmt: skip
    if res.returncode != 0 and "is not a docker command" in res.stderr:
        pytest.skip("docker compose plugin not installed")
    assert res.returncode == 0, res.stderr
    data: dict[str, Any] = json.loads(res.stdout)
    return data


def check_isolated(cfg: dict[str, Any], app: str, published: str) -> None:
    services = cfg["services"]
    assert cfg["networks"]["internal"]["internal"] is True
    for name, svc in services.items():
        if name == "ui":
            continue
        nets = set(svc.get("networks", {}))
        assert nets <= {"internal"}, f"{name} has a route out: {nets}"
        assert not svc.get("ports"), f"{name} publishes ports"
    link = services["cairnward-link"]
    assert link["network_mode"] == f"service:{app}"
    assert link["volumes"][0]["source"] == "/state/clients"
    assert "bind=127.0.0.1" in link["command"][0]
    (port,) = services["ui"]["ports"]
    assert port["host_ip"] == "127.0.0.1" and port["published"] == published


def test_open_webui() -> None:
    cfg = compose(ROOT / "open-webui" / "compose.yaml", env=ENV)
    check_isolated(cfg, "open-webui", "3000")
    env = cfg["services"]["open-webui"]["environment"]
    assert env["OPENAI_API_BASE_URL"] == "http://127.0.0.1:8484/v1"
    assert env["OFFLINE_MODE"] == "true" and env["ENABLE_OLLAMA_API"] == "false"
    missing = subprocess.run([shutil.which("docker") or "docker", "compose", "-f",
                              str(ROOT / "open-webui" / "compose.yaml"), "config"],
                             capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
                             check=False)  # fmt: skip
    assert missing.returncode != 0 and "required variable" in missing.stderr  # pins required


def test_librechat_override(tmp_path: Path) -> None:
    # a stand-in for LibreChat's own docker-compose.yml (its service names and defaults)
    base = tmp_path / "docker-compose.yml"
    base.write_text(
        "services:\n"
        "  api: {image: librechat, ports: ['3080:3080']}\n"
        "  mongodb: {image: mongo}\n  meilisearch: {image: meili}\n"
        "  vectordb: {image: pgvector}\n  rag_api: {image: rag}\n"
    )
    cfg = compose(base, ROOT / "librechat" / "docker-compose.override.yml", env=ENV)
    check_isolated(cfg, "api", "3080")
    assert cfg["services"]["cairnward-link-rag"]["network_mode"] == "service:rag_api"
