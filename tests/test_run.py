import json
import sys
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
from cairnward.run.runner import _install_dirs, program_reads
from cairnward.sandbox import SandboxSpec, secret_dirs, srt_hosts, wrap
from cairnward.sandbox.srt import srt_dirs


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


def test_run_refuses_a_model_from_another_environment(paths: Paths) -> None:
    from cairnward.run import run_agent
    from cairnward.run.runner import check_model_environment

    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "other.toml").write_text("")
    cfg = cfg_with(paths, 'isolation = "off"')
    with pytest.raises(RunError, match="in environment 'other' but this run is in 'e'"):
        run_agent(cfg, command=["no-such-program"], environment="e", model="cw/other:high")
    with pytest.raises(RunError, match="in environment 'other'"):
        check_model_environment(cfg, "e", "cw/other")
    # these reach the gateway in the run's environment, or get its own 404
    for fine in ("cw/e:high", "cw/e", "cw/raw/qwen3:30b", "cw/nowhere:high", "not-virtual"):
        check_model_environment(cfg, "e", fine)
    with pytest.raises(RunError, match="command not found"):
        run_agent(cfg, command=["no-such-program"], environment="e", model="cw/e:high")


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
            # Read-only views for an observer (D82): no keys or tokens in them.
            (listed,) = (await c.get("/v1/runs")).json()["runs"]
            assert listed["run_id"] == run["run_id"] and listed["environment"] == "e"
            assert run["gateway_key"] not in json.dumps(listed)
            since = (await c.get("/v1/audit/since", params={"seq": 0})).json()
            assert [r["action"] for r in since["records"]][-1] == "run.start"
            assert since["head_seq"] == since["records"][-1]["seq"]
            assert since["records"][-1]["hash"] == audit.head()[1]
            later = (await c.get("/v1/audit/since", params={"seq": since["head_seq"]})).json()
            assert later["records"] == []
            assert (await c.get("/v1/audit/since", params={"limit": 5000})).status_code == 422
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
    if sys.platform != "win32":
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


# ------------------------------------------------------------------ what a run can read


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if not sys.platform.startswith("linux"):
        pytest.skip("OS isolation is Linux only, so nothing else plans a hidden home folder")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CAIRNWARD_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    return home


def plan_in(paths: Paths, privacy: str, tmp_path: Path, workspace: Path, command: list[str]) -> Any:
    run_tmp = tmp_path / "run"  # beside the fake home, as the real one is
    run_tmp.mkdir(exist_ok=True)
    return plan_run(cfg_with(paths, privacy), environment="e", profile=PROFILES["aider"],
                    model="cw/e:medium", workspace=workspace, command=command, run=RUN,
                    srt=["srt"], settings_file=run_tmp / "srt.json",
                    tmp_dir=run_tmp)  # fmt: skip


def test_run_hides_the_home_folder(paths: Paths, tmp_path: Path, home: Path) -> None:
    ws = home / "projects" / "ws"
    ws.mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n")
    (home / ".aider.conf.yml").write_text("")
    (home / ".env").write_text("KEY=1\n")
    (home / ".config" / "rclone").mkdir(parents=True)
    (home / ".cache" / "cairnward" / "paperqa").mkdir(parents=True)
    p = plan_in(paths, 'preset = "strict"', tmp_path, ws, ["sh"])
    assert home in p.spec.deny_read
    allowed = p.spec.allow_read
    assert ws in allowed and tmp_path / "run" in allowed and home / ".aider" in allowed
    assert home / ".gitconfig" in allowed and home / ".aider.conf.yml" in allowed
    assert home not in allowed and home / ".env" not in allowed
    assert home / ".config" / "rclone" in p.spec.deny_read
    # the profile may write ~/.cache, but not PaperQA2's index and pickle cache in it
    assert home / ".cache" / "cairnward" / "paperqa" in p.spec.deny_read
    assert paths.data_dir / "sandbox-runtime" in allowed
    assert not any("home_access" in w for w in p.warnings)


def test_runs_can_not_read_the_backup_or_shared_folders(
    paths: Paths, tmp_path: Path, home: Path
) -> None:
    ws = home / "ws"
    ws.mkdir()
    backups, shared = tmp_path / "bk", tmp_path / "shared"
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "cairnward.toml").write_text(
        f'[backup]\nrepository = "{backups}"\n[replication]\nfolder = "{shared}"\n'
    )
    for privacy in ('preset = "strict"', 'preset = "strict"\nhome_access = "readable"'):
        deny = plan_in(paths, privacy, tmp_path, ws, ["sh"]).spec.deny_read
        assert backups in deny and shared in deny
    # a project kept inside the folder still works
    inside = shared / "project"
    inside.mkdir(parents=True)
    assert (
        shared not in plan_in(paths, 'preset = "strict"', tmp_path, inside, ["sh"]).spec.deny_read
    )
    # with no folder set, backups' default is what's hidden; a remote has no folder to hide
    (paths.config_dir / "cairnward.toml").write_text("")
    assert (
        home / "cairnward-backups"
        in plan_in(paths, 'preset = "strict"', tmp_path, ws, ["sh"]).spec.deny_read
    )
    (paths.config_dir / "cairnward.toml").write_text('[backup]\nrepository = "s3:s3.example/b"\n')
    plan_in(paths, 'preset = "strict"', tmp_path, ws, ["sh"])


def test_run_home_opt_out_warns(paths: Paths, tmp_path: Path, home: Path) -> None:
    ws = home / "ws"
    ws.mkdir()
    p = plan_in(paths, 'preset = "strict"\nhome_access = "readable"', tmp_path, ws, ["sh"])
    assert home not in p.spec.deny_read and home / ".ssh" in secret_dirs()
    assert any("home_access = readable" in w for w in p.warnings)
    eff = cfg_with(paths, 'preset = "strict"\nhome_access = "readable"').privacy("e")
    assert "home_access" in eff.weaker_than_strict  # shown by privacy show and the dashboard
    assert cfg_with(paths, 'preset = "balanced"').privacy("e").value("home_access") == "hidden"


@pytest.mark.parametrize("above", [False, True])
def test_workspace_holding_the_home_folder_says_so(
    paths: Paths, tmp_path: Path, home: Path, above: bool
) -> None:
    (home / ".ssh").mkdir()
    p = plan_in(paths, 'preset = "strict"', tmp_path, home.parent if above else home, ["sh"])
    assert home not in p.spec.deny_read  # a re-allowed workspace would undo it anyway
    assert home / ".ssh" in p.spec.deny_read
    assert any("contains your home folder" in w for w in p.warnings)


def test_srt_package_stays_readable(tmp_path: Path, paths: Paths) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "dist" / "cli.js").write_text("")
    assert srt_dirs(paths, ["node", str(pkg / "dist" / "cli.js")]) == [
        paths.data_dir / "sandbox-runtime", pkg.resolve()]  # fmt: skip
    assert srt_dirs(paths, ["srt-not-on-path"]) == [paths.data_dir / "sandbox-runtime"]


def executable(path: Path, text: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def test_program_reads_follow_links_and_interpreters(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    # as `uv tool install` lays it out: link -> tool's script -> tool's python -> a runtime
    runtime = executable(home / "rt" / "cpython" / "bin" / "python3")
    (home / "rt" / "cpython" / "lib").mkdir()
    tool = home / "tools" / "aider"
    (tool / "bin").mkdir(parents=True)
    (tool / "pyvenv.cfg").write_text("")
    (tool / "bin" / "python").symlink_to(runtime)
    script = executable(tool / "bin" / "aider", f"#!{tool}/bin/python\n")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "aider").symlink_to(script)
    bin_dir = str(home / ".local" / "bin")
    reads = program_reads(["aider"], bin_dir, workspace=ws, home=home)
    assert {tool, home / "rt" / "cpython", home / ".local" / "bin"} <= set(reads)
    # `#!/usr/bin/env -S node --flag` is looked up on PATH
    node = executable(home / "fnm" / "v22" / "bin" / "node")
    (home / "fnm" / "v22" / "lib").mkdir()
    qwen = executable(home / "npm" / "qwen", "#!/usr/bin/env -S node --flag\n")
    reads = program_reads([str(qwen)], str(node.parent), workspace=ws, home=home)
    assert home / "fnm" / "v22" in reads and home / "npm" in reads


def test_program_reads_stay_narrow(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    in_home = executable(home / "agent")
    assert program_reads([str(in_home)], "", workspace=ws, home=home) == [in_home]
    # ~/bin, ~/.local and a prefix that's no install: never the folder around them
    assert _install_dirs(home / "bin" / "x", home) == [home / "bin"]
    local = _install_dirs(home / ".local" / "bin" / "x", home)
    assert local == [home / ".local" / "bin", home / ".local" / "lib", home / ".local" / "lib64"]
    (home / ".cargo" / "bin").mkdir(parents=True)
    assert _install_dirs(home / ".cargo" / "bin" / "x", home) == [home / ".cargo" / "bin"]
    # PATH holding the home folder itself doesn't open it, nor does a file argument
    assert program_reads(["sh"], str(home), workspace=ws, home=home) == []
    (home / "private").mkdir()
    (home / "private" / "notes.md").write_text("")
    notes = str(home / "private" / "notes.md")
    assert program_reads(["/bin/true", notes], "", workspace=ws, home=home) == []
    # an interpreter's script does need its folder (and only the file when it's in home)
    assert home / "private" in program_reads(["/bin/python3", notes], "", workspace=ws, home=home)
    # the active venv
    venv = home / "work" / ".venv"
    reads = program_reads(["/bin/true"], "", workspace=ws, home=home, virtual_env=str(venv))
    assert reads == [venv]
    assert program_reads(["/bin/true"], "", workspace=ws, home=home, virtual_env=str(home)) == []


def test_re_allowed_paths_are_only_the_outermost(paths: Paths, tmp_path: Path, home: Path) -> None:
    runtime = home / ".local/share/uv/python/cpython-3.12.16/bin"
    runtime.mkdir(parents=True)
    executable(runtime / "python3")
    link = home / ".local/share/uv/python/cpython-3.12"
    link.symlink_to("cpython-3.12.16")
    prog = executable(home / "tool" / "agent", f"#!{link}/bin/python3\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    allowed = plan_in(paths, 'preset = "strict"', tmp_path, ws, [str(prog)]).spec.allow_read
    assert home / ".local/share/uv/python" in allowed and home / "tool" in allowed
    assert link not in allowed  # bwrap refuses a mount on a link inside a re-allowed folder


def test_secret_list_covers_what_runs_could_read(home: Path) -> None:
    secrets = secret_dirs()
    for name in (".config/rclone", ".bash_history", ".zsh_history", ".pypirc", ".npmrc",
                 ".cargo/credentials", ".cargo/credentials.toml", ".thunderbird",
                 ".config/BraveSoftware", ".librewolf", ".config/Code", ".config/Signal",
                 ".var/app", ".ssh", ".config/git/credentials"):  # fmt: skip
        assert home / name in secrets, name
