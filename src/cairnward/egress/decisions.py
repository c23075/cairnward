"""The decision service: policy for an egress data path in another process (ADR-0020, stage 1).

The Rust data path (`cairnward-egress-rs`, stage 2) handles connections, parsing, DNS and the
address checks itself, and asks this service, on a Unix socket, everything that needs policy,
approvals or the audit log. The answers come from the same `EgressProxy` methods the Python data
path uses, so there is one implementation of each decision, not two.

Protocol: one JSON object per line each way; every request gets exactly one reply, in order.
A `ticket` names one connection from `connect` until `closed` or `denied`.

- `{"op": "session", "authorization": <Proxy-Authorization header or null>}` ->
  `{"ok": true, "valid": bool}`: is this a known session token? Asked first, so a client without
  one gets 407 before anything else about its request is looked at, as in the Python path.
- `{"op": "connect", "authorization": <Proxy-Authorization header or null>, "method": str,
   "host": str, "port": int}`
  -> `{"ok": true, "allow": bool, "status": 200|403|407, "reason": str, "ticket": str|null,
       "inspect": bool, "any_environment": bool}`
  407: no or unknown session token. 403: denied by policy (already audited as `deny`). On
  allow, `inspect` says whether TLS inspection applies (stage 3; until then the data path refuses
  an inspecting session rather than tunnelling it unread).
- `{"op": "allow_special", "ticket": str, "address": str}` -> `{"ok": true, "allow": bool}`:
  may this special address (loopback, link-local...; the data path judged it) be dialled
  because it's listed exactly?
- `{"op": "deny", "ticket": str, "reason": str}` -> `{"ok": true}`: the data path refused the
  connection after `connect` allowed it (DNS failed, only special addresses); audited as `deny`
  with high risk, as the Python path does. Ends the ticket.
- `{"op": "opened", "ticket": str}` -> `{"ok": true}`: dialled; writes the `pre` record.
- `{"op": "closed", "ticket": str, "outcome": "ok"|"error", "bytes_up": int,
   "bytes_down": int, "duration_s": float, "address": str, "error": str|null,
   "status": int (plain HTTP only)}` -> `{"ok": true}`: writes the `post` record. Ends the ticket.

TLS inspection (stage 3), for a ticket whose `connect` said `inspect`:
- `{"op": "certificate", "ticket": str}` -> `{"ok": true, "chain": PEM, "key": PEM}`: the host's
  certificate from Cairnward's local CA (leaf then CA) and its key.
- `{"op": "request", "ticket": str, "method": str, "target": str, "hosts": [str]}` ->
  `{"ok": true, "allow": bool, "reason": str, "request": str}`: may this request inside the
  tunnel go out? `hosts` are its Host header values. The method and path rules (with every
  reading of the path, I36), a CONNECT inside the tunnel, and a request naming another site
  (fronting, T-34) are judged here, as in `inspect.Inspector`.
- `{"op": "request_done", "ticket": str, "request": str, "status": int|null, "bytes_up": int,
   "bytes_down": int, "error": str|null}` -> `{"ok": true}`: one `net.request` audit record,
  with the path and query as a digest only.

- `{"op": "drop", "ticket": str}` -> `{"ok": true}`: end a ticket with no further record (a
  plain-HTTP request refused by the inspection rules, already recorded by `request_done`).

With a secret (as `serve` always runs it), each connection must first send
`{"op": "hello", "secret": str}` -> `{"ok": true}`; anything else, or a wrong secret, gets an
error and the connection is closed. In isolated mode the sandboxed core may connect to any Unix
socket, and the secret, given to the Rust binary on its stdin, is what keeps it from asking
here for certificates or recording on egress's behalf (threat-model review 2026-11-10, Rust
data path, R1).

Errors: `{"ok": false, "error": str}` (unknown op or ticket, malformed line); the connection
stays usable. Lines over 64 KiB close it. Only the owner can connect (0600 socket in a private
folder), and the service trusts what the data path reports about bytes and outcomes, as the
Python path trusts itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairnward.config.models import TlsInspection
from cairnward.egress.inspect import Exchange, Verdict, check_request, fronted, request_path
from cairnward.egress.proxy import EgressProxy, Session, _BadRequest, _split_hostport
from cairnward.policy.engine import Action, Decision

MAX_LINE = 64 * 1024


@dataclass
class _Ticket:
    action: Action
    decision: Decision
    session: Session
    host: str = ""
    port: int = 0
    inspection: TlsInspection | None = None
    pre_seq: int | None = None
    requests: dict[str, Exchange] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=lambda: {"requests": 0, "denied": 0})


@dataclass
class DecisionService:
    """Answers a data path's questions with `proxy`'s decision logic (never started itself)."""

    proxy: EgressProxy
    secret: str | None = None  # required as the first line of each connection when set
    _tickets: dict[str, _Ticket] = field(default_factory=dict)
    _server: asyncio.AbstractServer | None = None

    async def start(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, str(path), limit=MAX_LINE)
        path.chmod(0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        await self.proxy.close()  # cancels approval prompts still open

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self.secret is not None:
                hello: Any = None
                with contextlib.suppress(ValueError):
                    hello = json.loads(await reader.readline())
                given = str(hello.get("secret", "")) if isinstance(hello, dict) else ""
                if not (isinstance(hello, dict) and hello.get("op") == "hello"
                        and secrets.compare_digest(given, self.secret)):  # fmt: skip
                    writer.write(b'{"ok": false, "error": "not authorised"}\n')
                    await writer.drain()
                    return
                writer.write(b'{"ok": true}\n')
                await writer.drain()
            while line := await reader.readline():
                try:
                    reply = await self.answer(json.loads(line))
                except (ValueError, TypeError, KeyError) as exc:
                    reply = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        except (ValueError, ConnectionError):  # a line over the limit, or the peer went away
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def answer(self, req: dict[str, Any]) -> dict[str, Any]:
        """One reply for one request (also callable directly, without the socket)."""
        op = req.get("op")
        if op == "session":
            return {"ok": True, "valid": self.proxy.sessions.lookup(req.get("authorization"))
                    is not None}  # fmt: skip
        if op == "connect":
            return await self._connect(req)
        ticket = self._tickets.get(str(req.get("ticket")))
        if ticket is None:
            return {"ok": False, "error": f"unknown ticket for {op!r}"}
        if op == "allow_special":
            allowed = self.proxy._explicitly_allowed_ip(
                ticket.action, str(req["address"]), _port(ticket.action),
                ticket.session.any_environment)  # fmt: skip
            return {"ok": True, "allow": allowed}
        if op == "certificate":
            if ticket.inspection is None:
                return {"ok": False, "error": "this connection isn't inspected"}
            chain, key = await asyncio.to_thread(self.proxy._tls_contexts_pem, ticket.host)
            return {"ok": True, "chain": chain.decode(), "key": key.decode()}
        if op == "request":
            return self._request(ticket, req)
        if op == "request_done":
            ex = ticket.requests.pop(str(req["request"]))
            ex.status = int(req["status"]) if isinstance(req.get("status"), int) else None
            ex.bytes_up, ex.bytes_down = int(req["bytes_up"]), int(req["bytes_down"])
            ex.error = str(req["error"])[:200] if req.get("error") else None
            await self.proxy._record_request(ticket.action, ex)
            return {"ok": True}
        if op == "drop":  # ended without a connection record (already recorded as refused)
            self._tickets.pop(str(req["ticket"]))
            return {"ok": True}
        if op == "deny":
            self._tickets.pop(str(req["ticket"]))
            refused = Decision(ticket.action, "deny", "high", str(req["reason"])[:300])
            await asyncio.to_thread(self.proxy.engine.audit_deny, refused)
            return {"ok": True}
        if op == "opened":
            ticket.pre_seq = await asyncio.to_thread(self.proxy.engine.audit_pre, ticket.decision)
            return {"ok": True}
        if op == "closed":
            self._tickets.pop(str(req["ticket"]))
            details: dict[str, Any] = {
                "bytes_up": int(req["bytes_up"]), "bytes_down": int(req["bytes_down"]),
                "duration_s": round(float(req["duration_s"]), 3),
                "address": str(req["address"])[:64],
            }  # fmt: skip
            if isinstance(req.get("status"), int):  # plain HTTP: the response's status
                details["status"] = req["status"]
            if req.get("error"):
                details["error"] = str(req["error"])[:200]
            if ticket.inspection is not None:
                details.update(inspected=True, **ticket.counts)
            outcome = "ok" if req.get("outcome") == "ok" else "error"
            await asyncio.to_thread(self.proxy.engine.audit_post, ticket.action, ticket.pre_seq,
                                    outcome=outcome, details=details)  # fmt: skip
            return {"ok": True}
        return {"ok": False, "error": f"unknown op {op!r}"}

    async def _connect(self, req: dict[str, Any]) -> dict[str, Any]:
        refused = {"ok": True, "allow": False, "ticket": None, "inspect": False,
                   "any_environment": False}  # fmt: skip
        session = self.proxy.sessions.lookup(req.get("authorization"))
        if session is None:
            return {**refused, "status": 407, "reason": "missing or unknown session token"}
        method = str(req["method"]).upper()
        try:
            host, port = _split_hostport(f"{req['host']}:{int(req['port'])}", 443)
        except _BadRequest as exc:
            return {**refused, "status": 400, "reason": str(exc)}
        display = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        action = Action(
            kind="net.connect", target=display, environment=session.environment,
            client=session.client, run_id=session.run_id, actor=session.actor,
            details={"method": method, "via": "egress"},
        )  # fmt: skip
        decision = await self.proxy.connect_decision(session, action, display)
        if not decision.allowed:
            await asyncio.to_thread(self.proxy.engine.audit_deny, decision)
            return {**refused, "status": 403, "reason": decision.reason}
        ticket = secrets.token_urlsafe(12)
        inspection = self.proxy._inspection(session, host, port)
        self._tickets[ticket] = _Ticket(action, decision, session, host, port, inspection)
        inspect = self.proxy._inspection(session, host, port) is not None
        return {"ok": True, "allow": True, "status": 200, "reason": decision.reason,
                "ticket": ticket, "inspect": inspect,
                "any_environment": session.any_environment}  # fmt: skip

    def _request(self, ticket: _Ticket, req: dict[str, Any]) -> dict[str, Any]:
        """One request inside an inspected tunnel, judged as `inspect.Inspector.run` judges it."""
        if ticket.inspection is None:
            return {"ok": False, "error": "this connection isn't inspected"}
        method, target = str(req["method"])[:20], str(req["target"])
        hosts = [(b"host", str(h).encode("latin-1", "replace")) for h in req.get("hosts", [])]
        verdict = check_request(ticket.inspection, ticket.host, ticket.port, method,
                                request_path(target))  # fmt: skip
        if method.upper() == "CONNECT":
            verdict = Verdict(False, "a tunnel inside an inspected tunnel", None)
        elif why := fronted(hosts, target, ticket.host):
            verdict = Verdict(False, why, None)
        ticket.counts["requests"] += 1
        if not verdict.allowed:
            ticket.counts["denied"] += 1
        rid = secrets.token_urlsafe(8)
        ticket.requests[rid] = Exchange(method, target, verdict)
        return {"ok": True, "allow": verdict.allowed, "reason": verdict.reason, "request": rid}


def _port(action: Action) -> int:
    return int(action.target.rpartition(":")[2])
