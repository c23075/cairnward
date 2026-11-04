"""The plugin contract's rules, without a sandbox (the real flow is in
tests/isolation/test_plugin_contract.py)."""

import base64
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cairnward.config.loader import load_config
from cairnward.config.models import McpConfig, McpServer
from cairnward.config.paths import Paths
from cairnward.gateway.clients import RunKeys
from cairnward.mcp.launch import LaunchError, plan_launch
from cairnward.plugins import contract
from cairnward.plugins.services import PluginSessions, plugin_router
from cairnward.policy.engine import Action, PolicyEngine
from cairnward.policy.provenance import ProvenanceTracker

READ: dict[str, Any] = {"permissions": {"read": []}}


def plugin(**kw: Any) -> McpServer:
    return McpServer(kind="plugin", command=["x"], **{**READ, **kw})


def configured(tmp_path: Path, extra: str = "", privacy: str = "") -> Any:
    paths = Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "cairnward.toml").write_text(f'[privacy]\npreset = "balanced"\n{privacy}')
    code = tmp_path / "code"
    code.mkdir()
    (code / "a.py").write_text("x")
    (paths.config_dir / "mcp.toml").write_text(f"""
[servers.demo]
kind = "plugin"
command = ["x"]
pin_dir = "{code}"
sha256 = "{contract.tree_digest(code)}"
environments = ["coding"]
fetch_hosts = ["api.example.org"]
fetch_rate_per_minute = 3
{extra}
[servers.demo.permissions]
read = []
""")
    return load_config(paths)


@pytest.mark.parametrize(
    "bad",
    [
        {"permissions": {}},  # read is required
        {"permissions": {"read": [], "network": ["example.org"]}},  # outside: through fetch
        {"permissions": {"read": [], "network": ["127.0.0.1"]}},  # exact port
        {"fetch_hosts": ["*"]},
        {"pin_dir": "/x"},  # needs sha256
        {"contract_min": 3, "contract_max": 2},
    ],
)
def test_manifest_limits(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        McpServer(kind="plugin", command=["x"], **{**READ, **bad})


def test_plugin_fields_are_only_for_plugins() -> None:
    with pytest.raises(ValidationError):
        McpServer(command=["x"], fetch_hosts=["a.org"])
    plugin(permissions={"read": [], "network": ["127.0.0.1:23119"]})  # a local exception is fine


def test_tree_digest_sees_contents_names_and_links(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    first = contract.tree_digest(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.pyc").write_text("cache")
    assert contract.tree_digest(tmp_path) == first
    (tmp_path / "a.py").write_text("y")
    second = contract.tree_digest(tmp_path)
    assert second != first
    (tmp_path / "link").symlink_to("/etc/hostname")
    assert contract.tree_digest(tmp_path) != second


def test_contract_range() -> None:
    assert contract.contract_problem("p", plugin()) is None
    assert contract.contract_problem("p", plugin(contract_min=2, contract_max=3))


def test_weakenings_are_listed() -> None:
    cfg = McpConfig(servers={"p": plugin(unsandboxed=True, unpinned_tools=True)})
    text = " ".join(contract.weakenings(cfg))
    assert "outside the sandbox" in text and "held for review" in text
    assert contract.weakenings(McpConfig(servers={"p": plugin()})) == []


def test_plugin_turns_itself_on(tmp_path: Path) -> None:
    assert "demo" in configured(tmp_path).environments["coding"].mcp_servers


def test_launch_refuses_changed_or_unverified_code(tmp_path: Path) -> None:
    cfg = configured(tmp_path)
    with pytest.raises(LaunchError, match="hasn't passed"):
        plan_launch(cfg, "demo", "coding", srt=["srt"], egress_url=None, tmp_dir=tmp_path)
    (tmp_path / "code" / "a.py").write_text("changed")
    with pytest.raises(LaunchError, match="changed"):
        plan_launch(cfg, "demo", "coding", srt=["srt"], egress_url=None, tmp_dir=tmp_path)


def test_a_plugin_needs_the_sandbox_even_with_isolation_off(tmp_path: Path) -> None:
    cfg = configured(tmp_path, privacy='isolation = "off"\n')
    with pytest.raises(LaunchError, match="sandbox"):
        plan_launch(cfg, "demo", "coding", srt=None, egress_url=None, tmp_dir=tmp_path,
                    verifying=True)  # fmt: skip
    plan = plan_launch(cfg, "demo", "coding", srt=["srt"], egress_url="http://proxy",
                       tmp_dir=tmp_path, verifying=True)  # fmt: skip
    assert plan.sandboxed and plan.spec is not None
    assert plan.spec.allowed_hosts == [f"127.0.0.1:{cfg.main.server.gateway_port}"]
    assert "HTTP_PROXY" not in plan.env  # it has no route but the gateway


def test_unsandboxed_is_its_own_switch(tmp_path: Path) -> None:
    cfg = configured(tmp_path, extra="unsandboxed = true")
    plan = plan_launch(cfg, "demo", "coding", srt=None, egress_url=None, tmp_dir=tmp_path)
    assert not plan.sandboxed and any("outside the sandbox" in w for w in plan.warnings)


def test_output_from_a_plugin_makes_the_next_action_ask(tmp_path: Path) -> None:
    cfg = configured(tmp_path, extra='default_tool = "allow"', privacy='approvals = "off"\n')
    asked: list[Any] = []

    class Approver:
        def request(self, req: Any) -> Any:
            asked.append(req)
            return SimpleNamespace(granted=False, by="user", scope="once", secret=None)

    engine = PolicyEngine(cfg, approver=Approver())
    plain = Action("tool.call", "demo/a", environment="coding", client="c")
    assert engine.check(plain).allowed
    assert not asked
    after = engine.check(replace(plain, plugin_output=("research",)))
    assert not after.allowed and "research" in asked[0].reason
    assert not asked[0].rememberable


def test_a_refused_action_stays_refused_after_plugin_output(tmp_path: Path) -> None:
    cfg = configured(tmp_path, extra='default_tool = "deny"')
    engine = PolicyEngine(cfg)
    action = Action("tool.call", "demo/a", environment="coding", plugin_output=("research",))
    assert engine.check(action).decision == "deny"


def test_the_users_next_message_clears_plugin_output() -> None:
    tracker = ProvenanceTracker()
    tracker.note_plugin("c", None, "research")
    assert tracker.plugins("c", None) == ("research",)
    tracker.note_request("c", None, [{"role": "user", "content": "go on"}])
    assert tracker.plugins("c", None) == ()


@pytest.fixture
def services(tmp_path: Path) -> Any:
    cfg = configured(
        tmp_path, privacy='network = "open"\napprovals = "off"\nredaction = "enforce"\n'
    )
    sent: list[httpx.Request] = []

    def origin(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        headers = {"location": "https://elsewhere.example/"}
        return httpx.Response(302, headers=headers, stream=httpx.ByteStream(b"\x00bytes"))

    sessions = PluginSessions(RunKeys())
    key, run_id = sessions.issue(cfg, "demo", "coding")
    app = FastAPI()
    app.include_router(plugin_router(cfg, PolicyEngine(cfg), None, sessions,
                                     transport=httpx.MockTransport(origin)))  # fmt: skip
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return SimpleNamespace(client=client, sent=sent, sessions=sessions, run_id=run_id, cfg=cfg)


def test_fetch_passes_bytes_and_redirects_back(services: Any) -> None:
    got = services.client.post("/v1/cairnward/fetch", json={"url": "https://api.example.org/x"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == 302 and base64.b64decode(body["body_b64"]) == b"\x00bytes"
    assert len(services.sent) == 1  # the redirect isn't followed: it would be checked anew


def test_fetch_sets_the_host_header_itself(services: Any) -> None:
    services.client.post("/v1/cairnward/fetch", json={
        "url": "https://api.example.org/",
        "headers": {"Host": "other.example", "X-A": "1"}})  # fmt: skip
    assert services.sent[0].headers["host"] == "api.example.org"
    assert services.sent[0].headers["x-a"] == "1"


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("https://not-listed.example/", 403),
        ("https://u:p@api.example.org/", 400),
        ("file:///etc/passwd", 400),
        ("http://127.0.0.1:1/", 403),
    ],
)
def test_fetch_refuses(services: Any, url: str, status: int) -> None:
    assert services.client.post("/v1/cairnward/fetch", json={"url": url}).status_code == status
    assert not services.sent


def test_fetch_scans_what_it_sends(services: Any) -> None:
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    got = services.client.post("/v1/cairnward/fetch", json={
        "url": "https://api.example.org/", "method": "POST",
        "body_b64": base64.b64encode(f"key {secret}".encode()).decode()})  # fmt: skip
    assert got.status_code == 403 and "not sent" in got.text
    assert not services.sent


def test_fetch_is_rate_limited(services: Any) -> None:
    codes = [services.client.post("/v1/cairnward/fetch",
                                  json={"url": "https://api.example.org/"}).status_code
             for _ in range(4)]  # fmt: skip
    assert codes == [200, 200, 200, 429]


def test_services_need_a_plugins_key(services: Any) -> None:
    run_key = services.sessions.run_keys.issue("agent", "run-1", None, "coding")
    other = TestClient(services.client.app, headers={"Authorization": f"Bearer {run_key}"})
    assert other.get("/v1/cairnward/contract").status_code == 403
    assert TestClient(services.client.app).get("/v1/cairnward/contract").status_code == 403


def test_a_revoked_key_stops_working(services: Any) -> None:
    assert services.client.get("/v1/cairnward/contract").status_code == 200
    services.sessions.revoke(services.run_id)
    assert services.client.get("/v1/cairnward/contract").status_code == 403


def test_artifacts_stay_in_the_plugins_area(services: Any) -> None:
    content = base64.b64encode(b"notes").decode()
    assert services.client.post("/v1/cairnward/artifacts", json={
        "path": "a/b.md", "content_b64": content}).status_code == 200  # fmt: skip
    assert services.client.get("/v1/cairnward/artifacts").json() == {"files": ["a/b.md"]}
    back = services.client.get("/v1/cairnward/artifacts/content", params={"path": "a/b.md"})
    assert base64.b64decode(back.json()["content_b64"]) == b"notes"
    root = services.cfg.paths.data_dir / "artifacts"
    assert (root / "plugin" / "demo").is_dir()
    for bad in ("../../conversation/x/y", "/etc/passwd"):
        got = services.client.post("/v1/cairnward/artifacts", json={
            "path": bad, "content_b64": content})  # fmt: skip
        assert got.status_code == 400


def test_wrong_passphrases_are_limited(tmp_path: Path) -> None:
    from cairnward.store import Vault

    cfg = configured(tmp_path, privacy='approvals = "off"\n')
    sessions = PluginSessions(RunKeys())
    key, _ = sessions.issue(cfg, "demo", "coding")
    app = FastAPI()
    with Vault.open(cfg.paths) as vault:
        app.include_router(plugin_router(cfg, PolicyEngine(cfg), None, sessions, vault=vault))
        c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
        url = "/v1/cairnward/item-keys"
        made = c.post(url, json={"op": "lock", "item": "x", "secret": "a long passphrase"})
        assert made.status_code == 200, made.text
        again = c.post(url, json={"op": "open", "item": "x", "secret": "a long passphrase"})
        assert again.json() == made.json()
        assert vault.is_locked("plugin:demo:x") and not vault.is_locked("x")  # kept apart
        codes = [c.post(url, json={"op": "open", "item": "x", "secret": "wrong"}).status_code
                 for _ in range(6)]  # fmt: skip
        assert codes == [400, 400, 400, 400, 400, 429]


def test_a_plugins_key_cant_use_the_mcp_endpoint(tmp_path: Path) -> None:
    from cairnward.backends import Backends
    from cairnward.gateway.app import GatewayDeps, create_app
    from cairnward.gateway.clients import ClientKeys
    from cairnward.mcp.hub import McpHub
    from cairnward.mcp.launch import LocalLauncher
    from cairnward.mcp.pins import PinStore

    cfg = configured(tmp_path, privacy='approvals = "off"\n')
    engine = PolicyEngine(cfg)
    run_keys = RunKeys()
    sessions = PluginSessions(run_keys)
    key, _ = sessions.issue(cfg, "demo", "coding")
    hub = McpHub(cfg, engine, LocalLauncher(cfg, srt=None), PinStore.at(cfg.paths.data_dir))
    app = create_app(GatewayDeps(cfg, engine, Backends(cfg.backends),
                                 ClientKeys.at(cfg.paths.state_dir), None, run_keys, mcp=hub,
                                 plugins=sessions))  # fmt: skip
    with TestClient(app, base_url="http://127.0.0.1") as c:
        got = c.post("/mcp", headers={"Authorization": f"Bearer {key}"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})  # fmt: skip
    assert got.status_code == 401


def test_a_name_that_resolves_here_is_refused_without_egress(tmp_path: Path) -> None:
    cfg = configured(tmp_path, privacy='network = "open"\napprovals = "off"\n')
    (cfg.paths.config_dir / "mcp.toml").write_text(
        (cfg.paths.config_dir / "mcp.toml").read_text().replace("api.example.org", "localhost")
    )
    cfg = load_config(cfg.paths)
    sessions = PluginSessions(RunKeys())
    key, _ = sessions.issue(cfg, "demo", "coding")
    app = FastAPI()
    app.include_router(plugin_router(cfg, PolicyEngine(cfg), None, sessions))
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    got = c.post("/v1/cairnward/fetch", json={"url": "http://localhost:9/"})
    assert got.status_code == 403
