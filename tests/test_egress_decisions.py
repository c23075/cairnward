"""The decision service for an out-of-process egress data path (ADR-0020), over a real socket."""

import asyncio
import base64
import json
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.egress import EgressProxy, Session, Sessions
from cairnward.egress.decisions import DecisionService
from cairnward.policy import ApprovalAnswer, ApprovalRequest, PolicyEngine, ScriptedApprover

pytestmark = pytest.mark.skipif(not hasattr(asyncio, "start_unix_server"), reason="Unix sockets")


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer

    async def ask(self, req: dict[str, Any] | bytes) -> dict[str, Any]:
        data = req if isinstance(req, bytes) else json.dumps(req).encode()
        self.writer.write(data + b"\n")
        await self.writer.drain()
        reply: dict[str, Any] = json.loads(await self.reader.readline())
        return reply


def _auth(token: str) -> str:
    return "Basic " + base64.b64encode(f"cw:{token}".encode()).decode()


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    logs: list[AuditLog] = []

    def make(privacy: str, answer: Callable[[ApprovalRequest], ApprovalAnswer] | None = None,
             session: Session | None = None) -> tuple[DecisionService, AuditLog, str]:  # fmt: skip
        paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
        (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
        (paths.config_dir / "environments" / "e.toml").write_text(f"[privacy]\n{privacy}\n")
        cfg = load_config(paths)
        log = AuditLog.open(paths)
        logs.append(log)
        engine = PolicyEngine(cfg, approver=ScriptedApprover(
            answer or (lambda r: ApprovalAnswer(False))), audit=log)  # fmt: skip
        sessions = Sessions()
        token = sessions.create(session or Session(environment="e", client="t", actor="agent:t"))
        return DecisionService(EgressProxy(cfg, engine, sessions, port=0)), log, token

    yield make
    for log in logs:
        log.close()


def _run(service: DecisionService, script: Callable[[Client], Any]) -> Any:
    async def main() -> Any:
        folder = Path(tempfile.mkdtemp(prefix="cw-dec-", dir="/tmp"))  # short: socket paths
        sock = folder / "d.sock"
        await service.start(sock)
        assert sock.stat().st_mode & 0o777 == 0o600
        reader, writer = await asyncio.open_unix_connection(str(sock))
        try:
            return await script(Client(reader, writer))
        finally:
            writer.close()
            await service.close()

    return asyncio.run(main())


def test_unknown_token_and_policy_denial(setup: Callable[..., Any]) -> None:
    service, log, token = setup('network = "off"')

    async def script(c: Client) -> None:
        r = await c.ask({"op": "connect", "authorization": None, "method": "CONNECT",
                         "host": "example.org", "port": 443})  # fmt: skip
        assert r["status"] == 407 and not r["allow"] and r["ticket"] is None
        r = await c.ask({"op": "connect", "authorization": _auth(token), "method": "CONNECT",
                         "host": "example.org", "port": 443})  # fmt: skip
        assert r["status"] == 403 and "network = off" in r["reason"]

    _run(service, script)
    deny = [r for r in log.records() if r.decision == "deny"]
    assert len(deny) == 1 and deny[0].target == "example.org:443"


def test_allowed_connection_is_audited_pre_and_post(setup: Callable[..., Any]) -> None:
    service, log, token = setup('network = "allowlist"\nnetwork_allow_hosts = ["example.org"]')

    async def script(c: Client) -> None:
        r = await c.ask({"op": "connect", "authorization": _auth(token), "method": "connect",
                         "host": "EXAMPLE.org", "port": 443})  # fmt: skip
        assert r["allow"] and r["status"] == 200 and r["inspect"] is False
        ticket = r["ticket"]
        assert (await c.ask({"op": "opened", "ticket": ticket}))["ok"]
        closed = {"op": "closed", "ticket": ticket, "outcome": "ok", "bytes_up": 10,
                  "bytes_down": 2000, "duration_s": 0.5, "address": "93.184.215.14",
                  "error": None}  # fmt: skip
        assert (await c.ask(closed))["ok"]
        assert not (await c.ask(closed))["ok"]  # the ticket is gone

    _run(service, script)
    pre, post = [r for r in log.records() if r.action == "net.connect"]
    assert (pre.phase, post.phase, post.pre_seq) == ("pre", "post", pre.seq)
    assert post.details["bytes_down"] == 2000 and post.details["address"] == "93.184.215.14"
    assert pre.target == "example.org:443"


def test_special_addresses_and_refusals_after_connect(setup: Callable[..., Any]) -> None:
    hosts = 'network_allow_hosts = ["intranet.lan", "127.0.0.1:8080"]'
    service, log, token = setup(f'network = "allowlist"\n{hosts}')

    async def script(c: Client) -> None:
        r = await c.ask({"op": "connect", "authorization": _auth(token), "method": "GET",
                         "host": "intranet.lan", "port": 8080})  # fmt: skip
        ticket = r["ticket"]
        listed = {"op": "allow_special", "ticket": ticket, "address": "127.0.0.1"}
        assert (await c.ask(listed))["allow"] is True
        other = {"op": "allow_special", "ticket": ticket, "address": "169.254.169.254"}
        assert (await c.ask(other))["allow"] is False
        why = "intranet.lan resolves to a link-local address"
        assert (await c.ask({"op": "deny", "ticket": ticket, "reason": why}))["ok"]

    _run(service, script)
    deny = [r for r in log.records() if r.decision == "deny"]
    assert len(deny) == 1 and "link-local" in json.dumps(deny[0].details)


def test_a_run_approval_covers_its_later_connections(setup: Callable[..., Any]) -> None:
    asked: list[str] = []

    def answer(req: ApprovalRequest) -> ApprovalAnswer:
        asked.append(req.target)
        return ApprovalAnswer(True)

    service, _, token = setup('network = "ask"', answer,
                              Session(environment="e", client="t", run_id="run1"))  # fmt: skip

    async def script(c: Client) -> None:
        req = {"op": "connect", "authorization": _auth(token), "method": "CONNECT",
               "host": "docs.example", "port": 443}  # fmt: skip
        assert (await c.ask(req))["allow"]
        second = await c.ask(req)
        assert second["allow"] and "approved for this run" in second["reason"]

    _run(service, script)
    assert asked == ["docs.example:443"]  # asked once


def test_bad_requests_get_an_error_and_the_connection_stays_usable(
    setup: Callable[..., Any],
) -> None:
    service, _, token = setup('network = "off"')

    async def script(c: Client) -> None:
        assert not (await c.ask(b"{not json"))["ok"]
        assert not (await c.ask({"op": "launch_missiles"}))["ok"]
        assert not (await c.ask({"op": "opened", "ticket": "nope"}))["ok"]
        assert not (await c.ask({"op": "connect", "authorization": _auth(token)}))["ok"]
        r = await c.ask({"op": "connect", "authorization": _auth(token), "method": "CONNECT",
                         "host": "example.org", "port": 70000})  # fmt: skip
        assert r["status"] == 400
        r = await c.ask({"op": "connect", "authorization": _auth(token), "method": "CONNECT",
                         "host": "example.org", "port": 443})  # fmt: skip
        assert r["status"] == 403

    _run(service, script)


def test_with_a_secret_only_its_holder_is_answered(setup: Callable[..., Any]) -> None:
    """In isolated mode the sandboxed core can reach any Unix socket; the secret, which only the
    Rust binary gets (on stdin), keeps it from asking for certificates or forging records."""
    service, _, token = setup('network = "off"')
    service.secret = "s3cret-for-the-binary"
    connect = {"op": "connect", "authorization": _auth(token), "method": "CONNECT",
               "host": "example.org", "port": 443}  # fmt: skip

    async def script(c: Client) -> list[Any]:
        sock = service._server.sockets[0].getsockname() if service._server else ""
        out: list[Any] = [await c.ask(connect)]  # no hello: refused, and the connection closed
        out.append(await c.reader.read())
        for hello in (
            {"op": "hello", "secret": "guess"},
            {"op": "hello", "secret": "s3cret-for-the-binary"},
        ):
            reader, writer = await asyncio.open_unix_connection(sock)
            other = Client(reader, writer)
            out.append(await other.ask(hello))
            if out[-1]["ok"]:
                out.append(await other.ask(connect))
            writer.close()
        return out

    refused, rest, wrong, ok, answered = _run(service, script)
    assert refused == {"ok": False, "error": "not authorised"} and rest == b""
    assert wrong == {"ok": False, "error": "not authorised"}
    assert ok == {"ok": True} and answered["status"] == 403
