"""cairnward-egress: the only process with internet access (spec §3.2, ADR-0010, ADR-0013).

An HTTP proxy on the loopback interface. For each connection it:
1. identifies the session from the proxy credentials (token -> environment, client, run);
2. asks the policy engine about `net.connect host:port` (approvals run in a thread and
   time out to deny);
3. resolves the host and refuses addresses that point back at this machine, link-local
   or cloud-metadata ranges, unless that exact IP is explicitly allowed; then dials the
   address it checked (no second DNS lookup);
4. tunnels (CONNECT) or forwards (plain HTTP), and records a pre/post audit pair with
   byte counts and duration, or a deny event.

HTTPS content is never decrypted here: policy is per host (spec §3.2, T-17).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from cairnward.config.loader import Config
from cairnward.egress.addresses import is_ip_literal, special_reason
from cairnward.policy.engine import Action, Decision, PolicyEngine, host_matches

MAX_HEAD = 64 * 1024
HOP_BY_HOP = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}


@dataclass(frozen=True)
class Session:
    environment: str
    client: str | None = None
    run_id: str | None = None
    actor: str = "cairnward"
    # The core's own connections (model calls it already policy-checked under the right
    # environment): logged, address-checked, not decided twice.
    trusted_core: bool = False


@dataclass
class Sessions:
    """Proxy credentials. A client uses http://<anything>:<token>@127.0.0.1:<port>."""

    _by_token: dict[str, Session] = field(default_factory=dict)
    # Hosts the user approved for a run when the sandbox asked (run_id -> {"host:port"}).
    _approved: dict[str, set[str]] = field(default_factory=dict)

    def create(self, session: Session) -> str:
        token = secrets.token_urlsafe(24)
        self._by_token[token] = session
        return token

    def revoke(self, token: str) -> None:
        self._by_token.pop(token, None)

    def revoke_run(self, run_id: str) -> None:
        for token, s in list(self._by_token.items()):
            if s.run_id == run_id:
                del self._by_token[token]
        self._approved.pop(run_id, None)

    def approve(self, run_id: str, target: str) -> None:
        """The user allowed `target` (host:port) for this run (sandbox prompt)."""
        self._approved.setdefault(run_id, set()).add(target.lower())

    def approved(self, run_id: str | None, target: str) -> bool:
        return run_id is not None and target.lower() in self._approved.get(run_id, set())

    def lookup(self, header: str | None) -> Session | None:
        if not header or not header.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        _, _, token = decoded.partition(":")
        for known, session in self._by_token.items():
            if secrets.compare_digest(known, token):
                return session
        return None


class _BadRequest(Exception):
    pass


def _split_hostport(value: str, default_port: int) -> tuple[str, int]:
    if value.startswith("["):  # [IPv6]:port
        host, _, rest = value[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, port = value.rpartition(":")
        if not sep:
            host, port = value, ""
    if not host:
        raise _BadRequest("missing host")
    try:
        port_n = int(port) if port else default_port
    except ValueError:
        raise _BadRequest(f"bad port in {value!r}") from None
    if not 0 < port_n < 65536:
        raise _BadRequest(f"bad port in {value!r}")
    return host.lower(), port_n


class EgressProxy:
    def __init__(
        self,
        config: Config,
        engine: PolicyEngine,
        sessions: Sessions,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        resolver: Callable[[str, int], Any] | None = None,
    ):
        self.config = config
        self.engine = engine
        self.sessions = sessions
        self.host = host
        self.port = config.main.server.egress_port if port is None else port
        self.limits = config.main.egress
        self._slots = asyncio.Semaphore(self.limits.max_connections)
        self._resolver = resolver
        self._server: asyncio.Server | None = None
        # Approval prompts are interactive; one at a time.
        self._ask_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, self.port,
                                                  limit=MAX_HEAD)  # fmt: skip
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        return self.port

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        if self._server is None:
            raise RuntimeError("egress proxy failed to start")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, reason: str, body: str = "",
                     headers: dict[str, str] | None = None) -> None:  # fmt: skip
        data = body.encode()
        lines = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(data)}",
                 "Content-Type: text/plain; charset=utf-8", "Connection: close"]  # fmt: skip
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
        with contextlib.suppress(ConnectionError):
            await writer.drain()

    async def _resolve(self, host: str, port: int) -> list[str]:
        if is_ip_literal(host):
            return [host.strip("[]")]
        if self._resolver is not None:
            return list(self._resolver(host, port))
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(str(info[4][0]) for info in infos))

    def _explicitly_allowed_ip(self, action: Action, ip: str, port: int) -> bool:
        eff = self.engine.privacy_for(action)
        target = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
        return any(host_matches(target, p) or host_matches(ip, p) for p in eff.network_allow_hosts)

    async def _decide(self, action: Action) -> Decision:
        try:
            async with self._ask_lock:
                return await asyncio.wait_for(
                    asyncio.to_thread(self.engine.check, action), self.limits.approval_timeout_s
                )
        except TimeoutError:
            rule = self.engine.rule_for(action)
            return Decision(action, "deny", rule.risk, f"{rule.reason}: approval timed out")

    async def _deny(self, writer: asyncio.StreamWriter, decision: Decision) -> None:
        await asyncio.to_thread(self.engine.audit_deny, decision)
        await self._reply(writer, 403, "Forbidden by cairnward",
                          f"cairnward-egress: {decision.action.target} denied: {decision.reason}\n",
                          {"X-Cairnward-Decision": "deny"})  # fmt: skip

    # ------------------------------------------------------------------ connection handling

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self._slots:
            try:
                await self._handle_one(reader, writer)
            except (_BadRequest, asyncio.LimitOverrunError, asyncio.IncompleteReadError) as exc:
                await self._reply(writer, 400, "Bad Request", f"cairnward-egress: {exc}\n")
            except (ConnectionError, TimeoutError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    writer.close()
                    await writer.wait_closed()

    async def _handle_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except ValueError as exc:  # header too large
            raise _BadRequest("request header too large") from exc
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            raise _BadRequest("malformed request line") from None
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep:
                raise _BadRequest("malformed header")
            headers.append((name.strip(), value.strip()))
        hmap = {k.lower(): v for k, v in headers}

        session = self.sessions.lookup(hmap.get("proxy-authorization"))
        if session is None:
            await self._reply(writer, 407, "Proxy Authentication Required",
                              "cairnward-egress: missing or unknown session token\n",
                              {"Proxy-Authenticate": 'Basic realm="cairnward"'})  # fmt: skip
            return

        if method.upper() == "CONNECT":
            host, port = _split_hostport(target, 443)
            path = None
        else:
            url = urlsplit(target)
            if url.scheme != "http" or not url.netloc:
                raise _BadRequest("only CONNECT and absolute http:// URLs are proxied")
            host, port = _split_hostport(url.netloc.rpartition("@")[2], 80)
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")

        display = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        action = Action(
            kind="net.connect", target=display, environment=session.environment,
            client=session.client, run_id=session.run_id, actor=session.actor,
            details={"method": method.upper(), "via": "egress"},
        )  # fmt: skip
        if session.trusted_core:
            decision = Decision(action, "allow", "low", "requested by the core (already checked)")
        elif self.sessions.approved(session.run_id, display):
            decision = Decision(action, "allow", self.engine.rule_for(action).risk,
                                "approved for this run when the sandbox asked",
                                approved_by="user", scope="run")  # fmt: skip
        else:
            decision = await self._decide(action)
            if decision.allowed and decision.asked and session.run_id:
                # a run's "yes" covers its later connections to this host too
                self.sessions.approve(session.run_id, display)
        if not decision.allowed:
            await self._deny(writer, decision)
            return

        # Resolve once, refuse special addresses, dial what was checked.
        try:
            addresses = await self._resolve(host, port)
        except OSError as exc:
            reason = f"DNS lookup failed: {exc.strerror or exc}"
            await self._deny(writer, Decision(action, "deny", decision.risk, reason))
            return
        chosen, refused = None, []
        for ip in addresses:
            why = special_reason(ip)
            if why is None or self._explicitly_allowed_ip(action, ip, port):
                chosen = ip
                break
            refused.append(why)
        if chosen is None:
            reason = f"{host} resolves to {refused[0] if refused else 'nothing'}"
            await self._deny(writer, Decision(action, "deny", "high", reason))
            return

        pre_seq = await asyncio.to_thread(self.engine.audit_pre, decision)
        started = time.monotonic()
        counts = {"bytes_up": 0, "bytes_down": 0}
        outcome, error = "ok", None
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(chosen, port), self.limits.connect_timeout_s
            )
        except (OSError, TimeoutError) as exc:
            outcome, error = "error", f"connect failed: {type(exc).__name__}"
            await self._reply(writer, 502, "Bad Gateway", f"cairnward-egress: {error}\n")
        else:
            try:
                if path is None:
                    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    await writer.drain()
                else:
                    out = [f"{method} {path} {version}"]
                    out += [f"{k}: {v}" for k, v in headers if k.lower() not in HOP_BY_HOP]
                    out.append("Connection: close")
                    first = ("\r\n".join(out) + "\r\n\r\n").encode()
                    up_writer.write(first)
                    counts["bytes_up"] += len(first)
                await self._pipe(reader, writer, up_reader, up_writer, counts)
            except (ConnectionError, TimeoutError) as exc:
                outcome, error = "error", type(exc).__name__
            finally:
                with contextlib.suppress(Exception):
                    up_writer.close()
        details: dict[str, Any] = {
            **counts,
            "duration_s": round(time.monotonic() - started, 3),
            "address": chosen,
        }
        if error:
            details["error"] = error
        await asyncio.to_thread(self.engine.audit_post, action, pre_seq, outcome=outcome,
                                details=details)  # fmt: skip

    async def _pipe(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
        counts: dict[str, int],
    ) -> None:
        idle = self.limits.idle_timeout_s

        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter, key: str) -> None:
            try:
                while True:
                    chunk = await asyncio.wait_for(src.read(65536), idle)
                    if not chunk:
                        break
                    counts[key] += len(chunk)
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionError, TimeoutError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    if dst.can_write_eof():
                        dst.write_eof()

        await asyncio.gather(
            copy(c_reader, u_writer, "bytes_up"), copy(u_reader, c_writer, "bytes_down")
        )
