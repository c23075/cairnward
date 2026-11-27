"""I39, D86: a client's own API key may be limited to some environments in clients.toml. A request
naming another one is refused and audited; `cairnward client add` limits a new client to the
default environment; doctor warns about keys that can reach a more open environment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from cairnward import cli
from cairnward.audit import AuditLog
from cairnward.audit.settings import diff, snapshot
from cairnward.backends import Backends
from cairnward.config import ConfigError, load_config
from cairnward.config.edit import EditError, limit_client
from cairnward.config.models import Client
from cairnward.config.paths import Paths
from cairnward.doctor import check_client_environments
from cairnward.gateway import ClientKeys, GatewayDeps, create_app
from cairnward.mcp.http import add_mcp_routes
from cairnward.policy import PolicyEngine
from tests.fakes.backends import FakeOllama

CHAT = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (p.config_dir / "environments").mkdir(parents=True)
    (p.config_dir / "environments" / "coding.toml").write_text('[privacy]\npreset = "strict"\n')
    (p.config_dir / "environments" / "loose.toml").write_text('[privacy]\npreset = "open"\n')
    (p.config_dir / "cairnward.toml").write_text('[defaults]\nenvironment = "coding"\n')
    return p


def clients(paths: Paths, text: str) -> Any:
    paths.clients_config.write_text(text)
    return load_config(paths)


# ------------------------------------------------------------------ configuration


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('kind = "chat"\nenvironments = ["nowhere"]', "unknown environment 'nowhere'"),
        ('kind = "chat"\nenvironments = []', "at least 1 item"),
        ('kind = "chat"\nenvironments = ["coding"]\ndefault_environment = "loose"',
         "default_environment 'loose' isn't in environments"),
    ],
)  # fmt: skip
def test_bad_limits_are_refused(paths: Paths, text: str, problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        clients(paths, f"[clients.app]\n{text}\n")


def test_home_environment_is_the_default_or_the_first_allowed() -> None:
    assert Client(kind="chat", environments=["b", "a"]).home_environment() == "b"
    assert (
        Client(kind="chat", environments=["b", "a"], default_environment="a").home_environment()
        == "a"
    )
    assert Client(kind="chat").home_environment() is None
    assert Client(kind="chat").may_use("anything")


# ------------------------------------------------------------------ the gateway


async def gateway(
    cfg: Any,
) -> tuple[httpx.AsyncClient, ClientKeys, AuditLog, FakeOllama, GatewayDeps]:
    fake = FakeOllama()
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fake.handler))
    audit = AuditLog.open(cfg.paths)
    keys = ClientKeys.at(cfg.paths.state_dir)
    deps = GatewayDeps(cfg, PolicyEngine(cfg, audit=audit), backends, keys)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(deps)),
                               base_url="http://127.0.0.1:8484")  # fmt: skip
    return client, keys, audit, fake, deps


async def test_a_limited_key_is_refused_elsewhere_and_audited(paths: Paths) -> None:
    cfg = clients(paths, '[clients.app]\nkind = "chat"\nenvironments = ["coding"]\n')
    c, keys, audit, fake, _ = await gateway(cfg)
    h = {"Authorization": f"Bearer {keys.add('app')}"}
    assert (await c.post("/v1/chat/completions", headers=h,
                         json={**CHAT, "model": "cw/coding:low"})).status_code == 200  # fmt: skip
    calls = len(fake.requests)
    for path, model, body in (
        ("/v1/chat/completions", "cw/loose:low", CHAT),
        ("/v1/embeddings", "cw/loose:embed", {"input": "hi"}),
    ):
        resp = await c.post(path, headers=h, json={**body, "model": model})
        assert resp.status_code == 403 and resp.json()["error"]["type"] == "client_environment"
        assert "may use coding, not 'loose'" in resp.json()["error"]["message"]
        last = audit.records()[-1]
        assert (last.action, last.decision, last.environment, last.client) == (
            "model.call",
            "deny",
            "loose",
            "app",
        )
    assert len(fake.requests) == calls  # never reached the model
    ids = [m["id"] for m in (await c.get("/v1/models", headers=h)).json()["data"]]
    assert any(i.startswith("cw/coding:") for i in ids)
    assert not any(i.startswith("cw/loose:") for i in ids)
    await c.aclose()
    audit.close()


async def test_unlimited_keys_and_runs_work_as_before(paths: Paths) -> None:
    cfg = clients(paths, '[clients.app]\nkind = "chat"\nenvironments = ["coding"]\n')
    c, keys, audit, _fake, deps = await gateway(cfg)
    free = {"Authorization": f"Bearer {keys.add('other')}"}  # not in clients.toml
    assert (await c.post("/v1/chat/completions", headers=free,
                         json={**CHAT, "model": "cw/loose:low"})).status_code == 200  # fmt: skip
    ids = [m["id"] for m in (await c.get("/v1/models", headers=free)).json()["data"]]
    assert any(i.startswith("cw/loose:") for i in ids)
    # a run is pinned to its own environment, which the owner chose when starting it
    run = {"Authorization": f"Bearer {deps.run_keys.issue('app', 'run-1', environment='loose')}"}
    assert (await c.post("/v1/chat/completions", headers=run,
                         json={**CHAT, "model": "cw/loose:low"})).status_code == 200  # fmt: skip
    ids = [m["id"] for m in (await c.get("/v1/models", headers=run)).json()["data"]]
    assert not any(i.startswith("cw/coding:") for i in ids)
    await c.aclose()
    audit.close()


async def test_mcp_refuses_an_environment_the_client_may_not_use(paths: Paths) -> None:
    cfg = clients(paths, '[clients.app]\nkind = "chat"\nenvironments = ["coding"]\n')
    app = FastAPI()
    add_mcp_routes(
        app, type("Hub", (), {"cfg": cfg})(),
        lambda request: ("app", None), default_environment=lambda c: "coding",
        run_environment=lambda r: None,
        client_may_use=lambda c, e: cfg.clients.clients[c].may_use(e),
    )  # fmt: skip
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1") as c:  # fmt: skip
        resp = await c.post("/mcp/loose", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 403 and "may not use 'loose'" in resp.text


def test_limits_are_audited(paths: Paths) -> None:
    before = snapshot(clients(paths, ""))
    after = snapshot(clients(paths, '[clients.app]\nkind = "chat"\nenvironments = ["coding"]\n'))
    limit = [{"client": "app", "environments": ["coding"]}]
    assert diff(before, after) == [
        {"scope": "clients", "setting": "client_environments", "before": [], "after": limit}
    ]


# ------------------------------------------------------------------ client add


def test_a_new_client_gets_the_default_environment(paths: Paths) -> None:
    note = limit_client(paths, "app", None)
    assert "may use: coding" in note
    assert load_config(paths).clients.clients["app"].environments == ["coding"]


def test_choices_when_adding(paths: Paths) -> None:
    assert "any environment" in limit_client(paths, "free", None, any_environment=True)
    assert not paths.clients_config.exists()
    limit_client(paths, "agent", ["loose", "coding"], kind="agent")
    added = load_config(paths).clients.clients["agent"]
    assert (added.kind, added.environments) == ("agent", ["loose", "coding"])


def test_an_existing_client_changes_only_when_asked(paths: Paths) -> None:
    clients(paths, '[clients.app]\nkind = "chat"\nprofile = "jan"\n')
    assert "can use any environment" in limit_client(paths, "app", None)  # a rotated key
    assert load_config(paths).clients.clients["app"].environments is None
    limit_client(paths, "app", ["loose"])
    changed = load_config(paths).clients.clients["app"]
    assert (changed.profile, changed.environments) == ("jan", ["loose"])


def test_a_bad_limit_leaves_the_file_as_it_was(paths: Paths) -> None:
    original = '[clients.app]\nkind = "chat"\n'
    clients(paths, original)
    with pytest.raises(EditError, match="unknown environment 'nowhere'"):
        limit_client(paths, "app", ["nowhere"])
    assert paths.clients_config.read_text() == original
    with pytest.raises(EditError):
        limit_client(paths, "new", ["nowhere"])
    assert paths.clients_config.read_text() == original


def test_cli_prints_the_key_on_the_second_line(
    paths: Paths, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    assert cli.main(["--config-dir", str(paths.config_dir), "client", "add", "app"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[1].startswith("cw-")  # scripts and tests read it from here
    assert "may use: coding" in err
    assert load_config(paths).clients.clients["app"].environments == ["coding"]


# ------------------------------------------------------------------ doctor


def test_doctor_warns_about_keys_that_reach_a_more_open_environment(paths: Paths) -> None:
    cfg = clients(paths, '[clients.limited]\nkind = "chat"\nenvironments = ["coding"]\n')
    ClientKeys.at(paths.state_dir).add("loosekey")
    checks = {c.name: c for c in check_client_environments(cfg)}
    assert set(checks) == {"client loosekey"}
    assert (
        checks["client loosekey"].status == "warn" and "loose" in checks["client loosekey"].detail
    )
    assert 'environments = ["coding"]' in checks["client loosekey"].fix


def test_doctor_is_quiet_when_no_environment_is_more_open(paths: Paths) -> None:
    for env in ("loose", "scratch"):  # scratch is built in, and more open by default
        (paths.config_dir / "environments" / f"{env}.toml").write_text(
            '[privacy]\npreset = "strict"\n'
        )
    ClientKeys.at(paths.state_dir).add("any")
    assert check_client_environments(load_config(paths)) == []
