"""Approvals inbox (M2): asking the user while a run is going, from the sandbox or egress."""

import asyncio
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cairnward import approvals_cli
from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.egress import Sessions
from cairnward.egress.control import LocalEgress, control_app
from cairnward.gateway.clients import RunKeys
from cairnward.policy import ApprovalInbox, ApprovalRequest, PolicyEngine, RemoteApprover
from cairnward.run import PROFILES, plan_run


def req(**kw: Any) -> ApprovalRequest:
    base: dict[str, Any] = dict(
        kind="net.connect", target="pypi.org:443", risk="medium", reason="network = ask",
        environment="e", conversation_id=None, client="aider", initiated_by="model",
        run_id="r1",
    )  # fmt: skip
    return ApprovalRequest(**{**base, **kw})


def ask_in_thread(inbox: ApprovalInbox, r: ApprovalRequest) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(inbox.request(r)), daemon=True)
    t.start()
    return t, out


def wait_pending(inbox: ApprovalInbox, n: int = 1) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        items = inbox.pending()
        if len(items) >= n:
            return items
        time.sleep(0.01)
    raise AssertionError("nothing arrived in the inbox")


# ------------------------------------------------------------------ inbox


def test_inbox_answer_and_notify() -> None:
    seen: list[dict[str, Any]] = []
    inbox = ApprovalInbox(5, notify=seen.append)
    v0 = inbox.version
    t, out = ask_in_thread(inbox, req())
    (item,) = wait_pending(inbox)
    assert inbox.version != v0
    assert item["target"] == "pypi.org:443" and item["run_id"] == "r1"
    assert seen and seen[0]["id"] == item["id"]
    assert inbox.answer(item["id"], True, "environment")
    t.join(5)
    assert out[0].granted and out[0].scope == "environment"
    assert inbox.pending() == []
    assert not inbox.answer(item["id"], False)  # gone


def test_inbox_timeout_is_no() -> None:
    inbox = ApprovalInbox(0.2)
    got = inbox.request(req())
    assert not got.granted and got.by == "timeout"
    assert inbox.pending() == []


def test_unrememberable_answers_become_once() -> None:
    inbox = ApprovalInbox(5)
    t, out = ask_in_thread(inbox, req(risk="high", rememberable=False))
    (item,) = wait_pending(inbox)
    inbox.answer(item["id"], True, "environment")
    t.join(5)
    assert out[0].scope == "once"


def test_wait_for_change() -> None:
    inbox = ApprovalInbox(5)
    v = inbox.version
    assert inbox.wait_for_change(v, 0.05) == v  # timed out, nothing new
    t, _ = ask_in_thread(inbox, req())
    assert inbox.wait_for_change(v, 5) != v
    inbox.answer(wait_pending(inbox)[0]["id"], False)
    t.join(5)


# ------------------------------------------------------------------ management API


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def cfg_with(paths: Paths, privacy: str) -> Any:
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "environments" / "e.toml").write_text(f"[privacy]\n{privacy}\n")
    return load_config(paths)


RUN_BODY = {
    "environment": "e",
    "client": "aider",
    "profile": "aider",
    "workspace": "/w",
    "program": "aider",
    "arg_count": 0,
    "sandbox": "srt",
}


async def _pending(c: httpx.AsyncClient) -> list[dict[str, Any]]:
    for _ in range(500):
        items: list[dict[str, Any]] = (await c.get("/v1/approvals")).json()["pending"]
        if items:
            return items
        await asyncio.sleep(0.01)
    raise AssertionError("no pending approval")


async def test_sandbox_ask_flow(paths: Paths) -> None:
    cfg = cfg_with(paths, 'network = "ask"')
    sessions, inbox = Sessions(), ApprovalInbox(10)
    with AuditLog.open(paths) as audit:
        engine = PolicyEngine(cfg, approver=inbox, audit=audit)
        app = management_app(cfg, audit=audit, run_keys=RunKeys(), egress=LocalEgress(sessions),
                             isolated=False, engine=engine, inbox=inbox)  # fmt: skip
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://cairnward"
        ) as c:
            run_id = (await c.post("/v1/runs", json=RUN_BODY)).json()["run_id"]
            ask = {"run_id": run_id, "host": "PyPI.org", "port": 443}

            # 1. asked, user says yes: allowed, remembered for the run, egress told
            task = asyncio.create_task(c.post("/v1/approvals/sandbox", json=ask))
            (item,) = await _pending(c)
            assert (item["kind"], item["target"], item["run_id"]) == (
                "net.connect", "pypi.org:443", run_id)  # fmt: skip
            assert (await c.post(f"/v1/approvals/{item['id']}", json={"granted": True})).json()
            assert (await task).json() == {"allow": True}
            assert sessions.approved(run_id, "pypi.org:443")
            # same host again: no second question
            assert (await c.post("/v1/approvals/sandbox", json=ask)).json() == {"allow": True}

            # 2. another host, user says no: denied with a reason
            task = asyncio.create_task(
                c.post("/v1/approvals/sandbox", json={**ask, "host": "evil.example"})
            )
            (item,) = await _pending(c)
            await c.post(f"/v1/approvals/{item['id']}", json={"granted": False})
            got = (await task).json()
            assert got["allow"] is False and "not approved" in got["reason"]

            # unknown answers and unknown runs
            assert (await c.post("/v1/approvals/nope", json={"granted": True})).status_code == 404
            got = (await c.post("/v1/approvals/sandbox", json={**ask, "run_id": "x"})).json()
            assert got["allow"] is False and "unknown" in got["reason"]

            await c.post(f"/v1/runs/{run_id}/end", json={"exit_code": 0, "duration_s": 1})
            assert not sessions.approved(run_id, "pypi.org:443")
        actions = [(r.action, r.decision) for r in audit.records()]
        assert ("approval", "allow") in actions and ("net.connect", "deny") in actions


async def test_sandbox_ask_follows_policy_without_asking(paths: Paths) -> None:
    cfg = cfg_with(paths, 'network = "allowlist"')
    inbox = ApprovalInbox(10)
    engine = PolicyEngine(cfg, approver=inbox)
    app = management_app(cfg, audit=None, run_keys=RunKeys(), egress=None, isolated=False,
                         engine=engine, inbox=inbox)  # fmt: skip
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cairnward"
    ) as c:
        run_id = (await c.post("/v1/runs", json=RUN_BODY)).json()["run_id"]
        ask = {"run_id": run_id, "host": "x.org", "port": 443}
        got = (await c.post("/v1/approvals/sandbox", json=ask)).json()
        assert got["allow"] is False and "allowlist" in got["reason"]
        assert inbox.version == 0  # nobody was asked


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.1", "2130706433", "::1",
                                  "169.254.169.254"])  # fmt: skip
async def test_sandbox_ask_refuses_local_addresses_unasked(paths: Paths, host: str) -> None:
    """The sandbox reaches this machine directly (not through egress), so egress's
    address guard is applied when it asks: only exactly listed addresses."""
    cfg = cfg_with(paths, 'network = "ask"\nnetwork_allow_hosts = ["127.0.0.1:9"]')
    inbox = ApprovalInbox(10)
    app = management_app(cfg, audit=None, run_keys=RunKeys(), egress=None, isolated=False,
                         engine=PolicyEngine(cfg, approver=inbox), inbox=inbox)  # fmt: skip
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cairnward"
    ) as c:
        run_id = (await c.post("/v1/runs", json=RUN_BODY)).json()["run_id"]
        got = (await c.post("/v1/approvals/sandbox",
                            json={"run_id": run_id, "host": host, "port": 80})).json()  # fmt: skip
        assert got["allow"] is False and "network_allow_hosts" in got["reason"]
        assert inbox.version == 0
        if host in ("localhost", "127.1"):  # listed: the policy decides (asks), as in egress
            ask = {"run_id": run_id, "host": host, "port": 9}
            task = asyncio.create_task(c.post("/v1/approvals/sandbox", json=ask))
            (item,) = await _pending(c)
            await c.post(f"/v1/approvals/{item['id']}", json={"granted": True})
            assert (await task).json() == {"allow": True}


def test_remote_approver_over_the_socket(paths: Paths, tmp_path: Path) -> None:
    """The egress process (isolated mode) asks through the core's management socket."""
    import uvicorn

    cfg = cfg_with(paths, "")
    inbox = ApprovalInbox(10)
    app = management_app(cfg, audit=None, run_keys=RunKeys(), egress=None, isolated=True,
                         engine=PolicyEngine(cfg, approver=inbox), inbox=inbox)  # fmt: skip
    sock = tmp_path / "m.sock"
    server = uvicorn.Server(uvicorn.Config(app, uds=str(sock), log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        for _ in range(500):
            if server.started:
                break
            time.sleep(0.01)
        remote = RemoteApprover(str(sock), 10)
        out: list[Any] = []
        t = threading.Thread(target=lambda: out.append(remote.request(req(provenance=("p",)))))
        t.start()
        (item,) = wait_pending(inbox)
        assert item["provenance"] == ["p"]
        inbox.answer(item["id"], True, "conversation")
        t.join(10)
        assert out[0].granted and out[0].scope == "conversation"
        # nobody listening: no
        assert not RemoteApprover(str(tmp_path / "none.sock"), 1).request(req()).granted
    finally:
        server.should_exit = True
        th.join(5)


async def test_egress_control_approve() -> None:
    sessions = Sessions()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_app(sessions)), base_url="http://egress"
    ) as c:
        await c.post("/v1/sessions/approve", json={"run_id": "r", "target": "A.org:443"})
    assert sessions.approved("r", "a.org:443")
    assert not sessions.approved("other", "a.org:443") and not sessions.approved(None, "a.org:443")
    sessions.revoke_run("r")
    assert not sessions.approved("r", "a.org:443")


# ------------------------------------------------------------------ runner


def _plan(paths: Paths, tmp_path: Path, privacy: str, srt: list[str]) -> Any:
    return plan_run(
        cfg_with(paths, privacy), environment="e", profile=PROFILES["generic"], model="m",
        workspace=tmp_path, command=["prog", "a b"],
        run={"run_id": "r1", "gateway_key": "k", "egress_token": "t", "gateway_port": 1,
             "egress_port": 2},
        srt=srt, settings_file=tmp_path / "srt.json", tmp_dir=tmp_path,
        ask_socket=tmp_path / "cairnward.sock",
    )  # fmt: skip


def test_runner_uses_ask_shim(paths: Paths, tmp_path: Path) -> None:
    pkg = tmp_path / "srt"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "dist" / "index.js").write_text("")
    srt = ["/usr/bin/node", str(pkg / "dist" / "cli.js")]
    p = _plan(paths, tmp_path, 'preset = "strict"\nnetwork = "ask"', srt)
    assert p.argv[1].endswith("srt_ask.mjs") and Path(p.argv[1]).exists()
    assert p.argv[2:7] == [str(pkg), str(tmp_path / "srt.json"),
                           str(tmp_path / "cairnward.sock"), "r1", "--"]  # fmt: skip
    assert p.argv[-2:] == ["prog", "a b"] and "no_proxy" in p.argv
    assert any("cairnward approvals" in w for w in p.warnings)
    # allowlist: the sandbox decides alone, no shim
    p = _plan(paths, tmp_path, 'preset = "strict"', srt)
    assert p.argv[:2] == srt
    # an srt that isn't the node package: plain srt, with a warning
    p = _plan(paths, tmp_path, 'preset = "strict"\nnetwork = "ask"', ["/usr/bin/srt"])
    assert p.argv[0] == "/usr/bin/srt" and any("can't ask" in w for w in p.warnings)


# ------------------------------------------------------------------ CLI


def test_cli_parse_and_describe() -> None:
    item = {"id": "a1", "kind": "net.connect", "target": "x.org:443", "risk": "medium",
            "reason": "network = ask", "environment": "e", "client": "aider", "run_id": "r1234567",
            "conversation_id": None, "initiated_by": "model", "provenance": ["web page"],
            "rememberable": True, "age_s": 2.0}  # fmt: skip
    text = approvals_cli.describe(item)
    assert "x.org:443" in text and "run r1234567" in text and "web page" in text
    assert "this run" in approvals_cli.options(item)
    assert approvals_cli.parse_answer("Y\n", item) == (True, "once")
    assert approvals_cli.parse_answer("e", item) == (True, "environment")
    assert approvals_cli.parse_answer("c", item) is None  # no conversation
    assert approvals_cli.parse_answer("no", item) == (False, "once")
    assert approvals_cli.parse_answer("maybe", item) is None
    assert approvals_cli.parse_answer("e", {**item, "rememberable": False}) is None


@pytest.mark.filterwarnings("ignore:You should not use the 'timeout'")
def test_cli_watch_answers(paths: Paths) -> None:
    cfg = cfg_with(paths, "")
    inbox = ApprovalInbox(10)
    app = management_app(cfg, audit=None, run_keys=RunKeys(), egress=None, isolated=False,
                         engine=PolicyEngine(cfg, approver=inbox), inbox=inbox)  # fmt: skip
    t, out = ask_in_thread(inbox, req())
    wait_pending(inbox)
    stdout = io.StringIO()
    with TestClient(app, base_url="http://cairnward") as c:
        code = approvals_cli.watch(c, io.StringIO("what\ne\n"), stdout, notify=False, once=True)  # type: ignore[arg-type]
        assert code == 0
        t.join(5)
        assert out[0].granted and out[0].scope == "environment"
        assert "not one of the options" in stdout.getvalue() and "allowed" in stdout.getvalue()
        listed = io.StringIO()
        approvals_cli.list_pending(c, listed, as_json=True)  # type: ignore[arg-type]
        assert json.loads(listed.getvalue()) == []
