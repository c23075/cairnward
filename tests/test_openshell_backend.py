"""sandbox = "openshell" (ADR-0018). The run flow is tested end to end against a stand-in
`openshell` CLI (tests/fakes/openshell_cli.py) and a real management API on a Unix socket;
no real OpenShell gateway was available, so its enforcement is not tested here."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.core.server import sockets
from cairnward.egress import Sessions
from cairnward.egress.control import LocalEgress
from cairnward.gateway.clients import RunKeys
from cairnward.run import PROFILES, RunError, plan_run, run_agent
from cairnward.sandbox import openshell

# Isolated runs are Linux-only here (Windows uses WSL), the management API is a Unix socket, and
# OpenShell itself runs on Linux, macOS and WSL.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="OpenShell runs need Linux (or WSL)")

FAKE = Path(__file__).parent / "fakes" / "openshell_cli.py"
RUN = {"run_id": "r1234567890abcdef", "gateway_key": "cw-run-k", "egress_token": "tok",
       "gateway_port": 8484, "egress_port": 8486}  # fmt: skip


@pytest.fixture
def paths() -> Iterator[Paths]:
    # short: the management socket lives in state_dir (Unix socket paths max out at 107 bytes)
    root = Path(tempfile.mkdtemp(prefix="cwos-"))
    yield Paths(root / "c", root / "d", root / "s")
    shutil.rmtree(root, ignore_errors=True)


def configure(paths: Paths, extra: str = "") -> Any:
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "environments" / "e.toml").write_text(
        f'sandbox = "openshell"\n{extra}\n[privacy]\nisolation = "on"\n'
    )
    return load_config(paths)


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "openshell-state"
    monkeypatch.setenv("CAIRNWARD_OPENSHELL", f"{shlex.quote(sys.executable)} {FAKE}")
    monkeypatch.setenv("FAKE_OPENSHELL_STATE", str(state))
    return state


def calls(state: Path) -> list[list[str]]:
    return [json.loads(line) for line in (state / "calls.jsonl").read_text().splitlines()]


# ------------------------------------------------------------------ pieces


def test_policy_allows_only_cairnwards_ports() -> None:
    p = openshell.policy("host.openshell.internal", [8484, 8486])
    rules = p["network_policies"]
    assert isinstance(rules, dict) and list(rules) == ["cairnward"]
    endpoints = rules["cairnward"]["endpoints"]
    assert endpoints == [
        {"host": "host.openshell.internal", "port": 8484, "protocol": "tcp"},
        {"host": "host.openshell.internal", "port": 8486, "protocol": "tcp"},
    ]
    assert p["version"] == 1 and p["landlock"] == {"compatibility": "hard_requirement"}


def test_env_file_survives_shell_quoting(tmp_path: Path) -> None:
    env = {"A": 'it\'s "quoted" $HOME `x`', "B": "two\nlines", "bad-name": "skipped"}
    f = tmp_path / "env.sh"
    f.write_text(openshell.env_script(env))
    out = subprocess.run(["sh", "-c", '. "$0" && printf %s "$A|$B"', str(f)],
                         capture_output=True, text=True, check=True)  # fmt: skip
    assert out.stdout == 'it\'s "quoted" $HOME `x`|two\nlines'
    assert "bad-name" not in f.read_text()


def test_mirror_back_writes_changes_and_deletes(tmp_path: Path) -> None:
    ws, copy = tmp_path / "ws", tmp_path / "copy"
    for root in (ws, copy):
        (root / "sub").mkdir(parents=True)
        (root / "same.txt").write_text("same")
    (ws / "changed.txt").write_text("old")
    (ws / "gone.txt").write_text("bye")
    (ws / "sub" / "gone-too.txt").write_text("bye")
    (copy / "changed.txt").write_text("new")
    (copy / "sub" / "new.txt").write_text("hello")
    (copy / "link").symlink_to("same.txt")
    before = openshell.manifest(ws)
    written, deleted, refused = openshell.mirror_back(copy, ws, before)
    assert (written, deleted, refused) == (3, 2, [])
    assert (ws / "changed.txt").read_text() == "new" and (ws / "sub" / "new.txt").exists()
    assert not (ws / "gone.txt").exists() and (ws / "link").readlink() == Path("same.txt")
    assert openshell.mirror_back(copy, ws, openshell.manifest(ws)) == (0, 0, [])


def test_mirror_back_never_writes_through_a_symlinked_folder(tmp_path: Path) -> None:
    outside, ws, copy = tmp_path / "outside", tmp_path / "ws", tmp_path / "copy"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside the workspace")
    ws.mkdir()
    (ws / "vendor").symlink_to(outside)  # the user's own link, made before the run
    (ws / "real").mkdir()
    (ws / "real" / "f").write_text("x")
    before = openshell.manifest(ws)
    (copy / "vendor").mkdir(parents=True)  # the agent replaced the link with a folder
    (copy / "vendor" / "evil.txt").write_text("written outside?")
    (copy / "real").symlink_to("/etc")  # and a folder with a link
    written, _, refused = openshell.mirror_back(copy, ws, before)
    assert not (outside / "evil.txt").exists() and (outside / "keep.txt").exists()
    assert "vendor/evil.txt" in refused and "real" in refused
    assert (ws / "real").is_dir() and not (ws / "real").is_symlink() and written == 0
    assert (ws / "real" / "f").exists()  # not deleted because its folder became a link


def test_plan_points_the_agent_at_this_machine_by_its_sandbox_name(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/home/someone")
    cfg = configure(paths, '[openshell]\nimage = "ghcr.io/example/agent:1"')
    plan = plan_run(cfg, environment="e", profile=PROFILES["opencode"], model="cw/e:medium",
                    workspace=tmp_path / "ws", command=["opencode"], run=RUN, srt=None,
                    settings_file=tmp_path / "srt.json", tmp_dir=tmp_path,
                    openshell_cli=["openshell"])  # fmt: skip
    assert plan.openshell is not None and plan.sandboxed and plan.argv == []
    env = plan.sandbox_env
    assert env is not None
    assert env["CAIRNWARD_MCP_URL"] == "http://host.openshell.internal:8484/mcp"
    assert env["HTTPS_PROXY"] == "http://cw:tok@host.openshell.internal:8486"
    assert "HOME" not in env and "PATH" not in env  # this machine's mean nothing in the image
    stage = tmp_path / openshell.RUN_DIR
    assert env["OPENCODE_CONFIG"] == str(stage / "opencode.json")  # rewritten on upload
    assert "host.openshell.internal:8484/v1" in (stage / "opencode.json").read_text()
    assert plan.openshell.image == "ghcr.io/example/agent:1"
    pol = json.loads(plan.openshell.policy_file.read_text())
    assert [e["port"] for e in pol["network_policies"]["cairnward"]["endpoints"]] == [8484, 8486]


def test_missing_cli_refuses(paths: Paths, tmp_path: Path) -> None:
    with pytest.raises(RunError, match="openshell command wasn't found"):
        plan_run(configure(paths), environment="e", profile=PROFILES["generic"],
                 model="cw/e:medium", workspace=tmp_path, command=["x"], run=RUN, srt=None,
                 settings_file=tmp_path / "s.json", tmp_dir=tmp_path,
                 openshell_cli=None)  # fmt: skip


def test_isolation_off_ignores_the_sandbox_choice(paths: Paths) -> None:
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "e.toml").write_text(
        'sandbox = "openshell"\n[privacy]\nisolation = "off"\n'
    )
    from cairnward.run.runner import uses_openshell

    assert not uses_openshell(load_config(paths), "e")


def test_tls_inspection_ca_goes_into_the_sandbox(paths: Paths, tmp_path: Path) -> None:
    cfg = configure(paths, "[tls_inspection]\nenabled = true")
    plan = plan_run(cfg, environment="e", profile=PROFILES["generic"], model="cw/e:medium",
                    workspace=tmp_path / "ws", command=["x"], run=RUN, srt=None,
                    settings_file=tmp_path / "s.json", tmp_dir=tmp_path,
                    openshell_cli=["openshell"])  # fmt: skip
    stage = tmp_path / openshell.RUN_DIR
    assert plan.sandbox_env is not None
    assert plan.sandbox_env["SSL_CERT_FILE"] == str(stage / "cairnward-ca-bundle.pem")
    assert (stage / "cairnward-ca.pem").read_bytes().startswith(b"-----BEGIN CERTIFICATE")


# ------------------------------------------------------------------ a whole run


@pytest.fixture
def server(paths: Paths) -> Iterator[AuditLog]:
    """The real management API on its Unix socket, as `cairnward serve` would run it."""
    cfg = configure(paths)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLog.open(paths)
    app = management_app(cfg, audit=audit, run_keys=RunKeys(),
                         egress=LocalEgress(Sessions()), isolated=False)  # fmt: skip
    sock = sockets(cfg)["management"]
    srv = uvicorn.Server(uvicorn.Config(app, uds=str(sock), log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    yield audit
    srv.should_exit = True
    t.join(5)
    audit.close()


AGENT = r"""
import os, pathlib, json
p = pathlib.Path.cwd()
(p / "changed.txt").write_text("after")
(p / "gone.txt").unlink()
(p / "new" ).mkdir()
(p / "new" / "file.txt").write_text("made in the sandbox")
(p / "seen.json").write_text(json.dumps({k: os.environ.get(k) for k in
    ("OPENAI_BASE_URL", "HTTPS_PROXY", "CAIRNWARD_API_KEY", "SECRET_ON_HOST")}))
raise SystemExit(3)
"""


def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "changed.txt").write_text("before")
    (ws / "gone.txt").write_text("bye")
    (ws / "agent.py").write_text(AGENT)
    return ws


def test_run_in_openshell_copies_work_back_and_cleans_up(
    paths: Paths, server: AuditLog, fake_cli: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setenv("SECRET_ON_HOST", "must not reach the sandbox")
    ws = workspace(tmp_path)
    lines: list[str] = []
    code = run_agent(load_config(paths), command=[sys.executable, "agent.py"], environment="e",
                     workspace=ws, log=lines.append)  # fmt: skip
    assert code == 3  # the agent's own exit code
    assert (ws / "changed.txt").read_text() == "after"
    assert not (ws / "gone.txt").exists()
    assert (ws / "new" / "file.txt").read_text() == "made in the sandbox"
    seen = json.loads((ws / "seen.json").read_text())
    assert seen["OPENAI_BASE_URL"] == "http://host.openshell.internal:8484/v1"
    assert seen["HTTPS_PROXY"].endswith("@host.openshell.internal:8486")
    assert seen["CAIRNWARD_API_KEY"] and seen["SECRET_ON_HOST"] is None
    assert not (ws / openshell.RUN_DIR).exists()  # the run's settings stay in the sandbox

    made = calls(fake_cli)
    assert [c[:2] for c in made] == [["sandbox", "create"], ["sandbox", "exec"],
                                     ["sandbox", "upload"], ["sandbox", "upload"],
                                     ["sandbox", "exec"], ["sandbox", "download"],
                                     ["sandbox", "delete"]]  # fmt: skip
    key = seen["CAIRNWARD_API_KEY"]
    assert not any(key in arg for c in made for arg in c)  # never on a command line
    assert not (fake_cli / "sandboxes").exists() or not any((fake_cli / "sandboxes").iterdir())
    start = next(r for r in server.records() if r.action == "run.start")
    end = next(r for r in server.records() if r.action == "run.end")
    assert start.details["sandbox"] == "openshell"
    # snapshots need git: without it the run still works, but doesn't count changed files
    assert end.details["changed_files"] == (4 if shutil.which("git") else None)
    assert any("2 written, 1 deleted" in line or "written" in line for line in lines)


def test_failed_download_keeps_the_sandbox_and_the_workspace(
    paths: Paths, server: AuditLog, fake_cli: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "download")
    ws = workspace(tmp_path)
    lines: list[str] = []
    run_agent(load_config(paths), command=[sys.executable, "agent.py"], environment="e",
              workspace=ws, log=lines.append)  # fmt: skip
    assert (ws / "changed.txt").read_text() == "before" and (ws / "gone.txt").exists()
    assert any("is kept" in line for line in lines)
    assert ["sandbox", "delete"] not in [c[:2] for c in calls(fake_cli)]


def test_failed_create_is_a_clean_refusal(
    paths: Paths, server: AuditLog, fake_cli: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "create")
    ws = workspace(tmp_path)
    with pytest.raises(RunError, match="openshell sandbox create failed"):
        run_agent(load_config(paths), command=[sys.executable, "agent.py"], environment="e",
                  workspace=ws, log=lambda _: None)  # fmt: skip
    end = next(r for r in server.records() if r.action == "run.end")
    assert end.outcome == "error" and (ws / "gone.txt").exists()
    assert os.environ.get("FAKE_OPENSHELL_FAIL") == "create"
