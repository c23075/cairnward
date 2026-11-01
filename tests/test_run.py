import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.egress import Session, Sessions
from cairnward.egress.control import LocalEgress, control_app
from cairnward.gateway.clients import RunKeys
from cairnward.run import PROFILES, RunError, plan_run
from cairnward.sandbox import SandboxSpec, srt_hosts, wrap


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def cfg_with(paths: Paths, privacy: str) -> Any:
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "environments" / "e.toml").write_text(f"[privacy]\n{privacy}\n")
    return load_config(paths)


RUN = {
    "run_id": "r1",
    "gateway_key": "cw-run-k",
    "egress_token": "tok",
    "gateway_port": 8484,
    "egress_port": 8486,
}


def plan(
    paths: Paths, privacy: str, tmp_path: Path, srt: list[str] | None = None, profile: str = "aider"
) -> Any:
    return plan_run(
        cfg_with(paths, privacy),
        environment="e",
        profile=PROFILES[profile],
        model="cw/e:medium",
        workspace=tmp_path / "ws",
        command=["aider", "--yes"],
        run=RUN,
        srt=srt,
        settings_file=tmp_path / "srt.json",
        tmp_dir=tmp_path,
    )


def test_sandboxed_plan(paths: Paths, tmp_path: Path) -> None:
    p = plan(
        paths, 'preset = "strict"\nnetwork_allow_hosts = ["pypi.org", "*"]', tmp_path, srt=["srt"]
    )
    assert p.sandboxed
    assert p.argv == [
        "srt",
        "--settings",
        str(tmp_path / "srt.json"),
        "env",
        "-u",
        "no_proxy",
        "-u",
        "NO_PROXY",
        f"TMPDIR={tmp_path}",
        "aider",
        "--yes",
    ]
    settings = json.loads((tmp_path / "srt.json").read_text())
    assert settings["network"]["allowedDomains"] == ["127.0.0.1:8484", "pypi.org"]  # '*' dropped
    assert settings["filesystem"]["allowWrite"][0] == str(tmp_path / "ws")
    assert str(paths.data_dir / "sandbox-runtime") in settings["filesystem"]["allowRead"]
    env = p.env
    assert (
        env["OPENAI_API_BASE"] == "http://127.0.0.1:8484/v1" and env["OPENAI_API_KEY"] == "cw-run-k"
    )
    assert (
        env["AIDER_MODEL"] == "openai/cw/e:medium" and env["AIDER_WEAK_MODEL"] == "openai/cw/e:low"
    )
    assert env["HTTPS_PROXY"] == "http://cw:tok@127.0.0.1:8486"  # srt's parent proxy
    assert env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert "NO_PROXY" not in env


def test_clean_environment(paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    monkeypatch.setenv("GITHUB_TOKEN", "leak")
    p = plan(paths, 'preset = "strict"', tmp_path, srt=["srt"])
    assert "AWS_SECRET_ACCESS_KEY" not in p.env and "GITHUB_TOKEN" not in p.env
    assert "leak" not in json.dumps(p.env)


def test_isolation_on_without_sandbox_refuses(paths: Paths, tmp_path: Path) -> None:
    with pytest.raises(RunError, match="sandbox install"):
        plan(paths, 'preset = "strict"', tmp_path, srt=None)


def test_unsandboxed_plan_warns_and_reaches_gateway_directly(paths: Paths, tmp_path: Path) -> None:
    p = plan(paths, 'preset = "open"', tmp_path, profile="generic")
    assert not p.sandboxed and p.argv == ["aider", "--yes"]
    assert p.env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert any("unsandboxed" in w for w in p.warnings)


def test_network_ask_warning(paths: Paths, tmp_path: Path) -> None:
    p = plan(paths, 'preset = "balanced"', tmp_path, srt=["srt"])
    assert any("network = ask" in w for w in p.warnings)


def test_srt_helpers() -> None:
    assert srt_hosts(["*", "*.com", "*.arxiv.org", "pypi.org:443"]) == [
        "*.arxiv.org",
        "pypi.org:443",
    ]
    spec = SandboxSpec(
        ["a.example"], [Path("/w")], [Path("/definitely/missing")], allow_all_unix_sockets=True
    )
    s = spec.settings()
    assert s["filesystem"]["denyRead"] == []  # missing paths dropped (srt rejects them)
    assert s["network"]["allowAllUnixSockets"] is True
    assert wrap(["srt"], Path("/s.json"), ["x"])[-1] == "x"


# ------------------------------------------------------------------ management API


async def test_run_lifecycle(paths: Paths) -> None:
    cfg = cfg_with(paths, "")
    sessions, run_keys = Sessions(), RunKeys()
    with AuditLog.open(paths) as audit:
        app = management_app(
            cfg, audit=audit, run_keys=run_keys, egress=LocalEgress(sessions), isolated=False
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cairnward"
        ) as c:
            assert (await c.get("/v1/status")).json()["isolated"] is False
            body = {
                "environment": "e",
                "client": "aider",
                "profile": "aider",
                "workspace": "/w",
                "program": "aider",
                "arg_count": 1,
                "sandbox": "srt",
            }
            run = (await c.post("/v1/runs", json=body)).json()
            assert run_keys.identify(run["gateway_key"]) == ("aider", run["run_id"])
            auth = (
                "Basic "
                + __import__("base64").b64encode(f"x:{run['egress_token']}".encode()).decode()
            )
            sess = sessions.lookup(auth)
            assert sess is not None and sess.run_id == run["run_id"] and sess.actor == "agent:aider"
            assert (
                await c.post(
                    f"/v1/runs/{run['run_id']}/end", json={"exit_code": 3, "duration_s": 1.5}
                )
            ).status_code == 200
            assert run_keys.identify(run["gateway_key"]) is None
            assert sessions.lookup(auth) is None
            assert (
                await c.post("/v1/runs/nope/end", json={"exit_code": 0, "duration_s": 0})
            ).status_code == 404
            bad = {**body, "environment": "ghost"}
            assert (await c.post("/v1/runs", json=bad)).status_code == 404
        start, end = audit.records()
        assert (start.action, end.action, end.outcome) == ("run.start", "run.end", "error")
        assert start.details["sandbox"] == "srt" and end.details["exit_code"] == 3


async def test_egress_control_app() -> None:
    sessions = Sessions()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_app(sessions)), base_url="http://egress"
    ) as c:
        assert (await c.get("/v1/health")).json() == {"ok": True}
        token = (
            await c.post(
                "/v1/sessions", json={"environment": "e", "run_id": "r", "trusted_core": True}
            )
        ).json()["token"]
        assert sessions._by_token[token] == Session("e", None, "r", "cairnward", True)
        await c.post("/v1/sessions/revoke", json={"run_id": "r"})
        assert token not in sessions._by_token


def test_profiles_point_agents_at_the_gateway(tmp_path: Path) -> None:
    base, key, model, fast = (
        "http://127.0.0.1:8484/v1",
        "cw-run-k",
        "cw/coding:medium",
        "cw/coding:low",
    )
    aider = PROFILES["aider"].env(base, key, model, fast)
    assert aider["AIDER_EDIT_FORMAT"] == "diff" and aider["AIDER_ANALYTICS"] == "false"
    qwen = PROFILES["qwen-code"].env(base, key, model, fast)
    assert (qwen["OPENAI_BASE_URL"], qwen["OPENAI_MODEL"]) == (base, model)
    goose = PROFILES["goose"].env(base, key, model, fast)
    assert goose["OPENAI_HOST"] == "http://127.0.0.1:8484" and goose["GOOSE_MODEL"] == model
    oc = PROFILES["opencode"]
    assert oc.config is not None
    env = oc.config(base, key, model, fast, tmp_path)
    cfg = json.loads(Path(env["OPENCODE_CONFIG"]).read_text())
    assert cfg["model"] == f"cairnward/{model}" and cfg["small_model"] == f"cairnward/{fast}"
    assert cfg["provider"]["cairnward"]["options"] == {"baseURL": base, "apiKey": key}
    assert cfg["autoupdate"] is False and cfg["share"] == "disabled"
    assert Path(env["OPENCODE_CONFIG"]).stat().st_mode & 0o777 == 0o600


def test_profile_config_written_in_plan(paths: Paths, tmp_path: Path) -> None:
    p = plan_run(cfg_with(paths, 'preset = "strict"'), environment="e",
                 profile=PROFILES["opencode"], model="cw/e:medium", workspace=tmp_path / "ws",
                 command=["opencode", "run", "x"], run=RUN, srt=["srt"],
                 settings_file=tmp_path / "srt.json", tmp_dir=tmp_path)  # fmt: skip
    assert Path(p.env["OPENCODE_CONFIG"]).parent == tmp_path  # removed with the run folder
    assert "registry.npmjs.org" not in json.loads((tmp_path / "srt.json").read_text())["network"][
        "allowedDomains"]  # fmt: skip


def test_sandbox_debug_switch(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRNWARD_SANDBOX_DEBUG", "1")
    assert plan(paths, 'preset = "strict"', tmp_path, srt=["srt"]).env["SRT_DEBUG"] == "1"
