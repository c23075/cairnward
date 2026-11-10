"""The decision service: policy for an egress data path in another process (ADR-0020, stage 1).

The Rust data path (`cairnward-egress-rs`, stage 2) handles connections, parsing, DNS and the
address checks itself, and asks this service, on a Unix socket, everything that needs policy,
approvals or the audit log. The answers come from the same `EgressProxy` methods the Python data
path uses, so there is one implementation of each decision, not two.

Protocol: one JSON object per line each way; every request gets exactly one reply, in order.
A `ticket` names one connection from `connect` until `closed` or `denied`.

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
   "bytes_down": int, "duration_s": float, "address": str, "error": str|null}` -> `{"ok": true}`:
  writes the `post` record. Ends the ticket.

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

from cairnward.egress.proxy import EgressProxy, Session, _BadRequest, _split_hostport
from cairnward.policy.engine import Action, Decision

MAX_LINE = 64 * 1024


@dataclass
class _Ticket:
    action: Action
    decision: Decision
    session: Session
    pre_seq: int | None = None


@dataclass
class DecisionService:
    """Answers a data path's questions with `proxy`'s decision logic (never started itself)."""

    proxy: EgressProxy
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
            if req.get("error"):
                details["error"] = str(req["error"])[:200]
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
        self._tickets[ticket] = _Ticket(action, decision, session)
        inspect = self.proxy._inspection(session, host, port) is not None
        return {"ok": True, "allow": True, "status": 200, "reason": decision.reason,
                "ticket": ticket, "inspect": inspect,
                "any_environment": session.any_environment}  # fmt: skip


def _port(action: Action) -> int:
    return int(action.target.rpartition(":")[2])
