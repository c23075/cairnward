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

HTTPS content isn't decrypted unless the environment turns on `[tls_inspection]` (FR-NET-2,
ADR-0018, off by default): then a CONNECT is ended here with a certificate from Cairnward's
local CA, a verified TLS connection is opened to the host, and each HTTP/1.1 request is checked
against the environment's method and path rules (`inspect.py`). The core's own connections are
never inspected. Otherwise policy is per host (spec §3.2, T-17).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from cairnward.audit.log import digest
from cairnward.config.loader import Config
from cairnward.config.models import TlsInspection
from cairnward.egress.addresses import is_ip_literal, special_reason
from cairnward.egress.inspect import Exchange, Inspector, Verdict, check_request, request_path
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
    # The isolated core's one session: its connections were checked in their own
    # environment, which egress can't see (the sandbox's proxy chains with one
    # credential), so an address on this machine or the LAN is accepted when any
    # environment lists it exactly.
    any_environment: bool = False


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


_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


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
        upstream_ssl: ssl.SSLContext | None = None,
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
        self._checks: set[asyncio.Future[Decision]] = set()  # policy checks still running
        self._issuer: Any = None  # tls.Issuer, made when an environment first inspects
        self._upstream_ssl = upstream_ssl

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
        for check in list(self._checks):  # a prompt nobody will answer now
            check.cancel()
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

    def _explicitly_allowed_ip(self, action: Action, ip: str, port: int,
                               any_environment: bool = False) -> bool:  # fmt: skip
        target = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
        cfg = self.engine.config
        if any_environment:
            scopes = [cfg.privacy(), *(cfg.privacy(n) for n in cfg.environments)]
        else:
            scopes = [self.engine.privacy_for(action)]
        return any(host_matches(target, p) or host_matches(ip, p)
                   for eff in scopes for p in eff.network_allow_hosts)  # fmt: skip

    async def _decide(self, action: Action) -> Decision:
        """The policy decision, asking the user when needed, within approval_timeout_s.

        One prompt at a time: the lock is held until the approver really returns, not just
        until this connection stops waiting, so a prompt still on screen after a timeout
        doesn't get a second one next to it. A late answer is dropped, not remembered: the
        connection it was for has already been refused."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.limits.approval_timeout_s

        def timed_out(why: str) -> Decision:
            rule = self.engine.rule_for(action)
            return Decision(action, "deny", rule.risk, f"{rule.reason}: {why}")

        try:
            await asyncio.wait_for(self._ask_lock.acquire(), self.limits.approval_timeout_s)
        except TimeoutError:
            return timed_out("approval timed out (another approval is still open)")
        cancelled = threading.Event()

        def finished(f: asyncio.Future[Decision]) -> None:
            self._checks.discard(f)
            self._ask_lock.release()
            if not f.cancelled():
                f.exception()  # retrieved, so a late failure isn't reported as unhandled

        check = asyncio.ensure_future(
            asyncio.to_thread(self.engine.check, action, cancelled=cancelled)
        )
        self._checks.add(check)
        check.add_done_callback(finished)
        try:
            return await asyncio.wait_for(asyncio.shield(check), max(0.0, deadline - loop.time()))
        except TimeoutError:
            cancelled.set()
            return timed_out("approval timed out")

    async def _deny(self, writer: asyncio.StreamWriter, decision: Decision) -> None:
        await asyncio.to_thread(self.engine.audit_deny, decision)
        await self._reply(writer, 403, "Forbidden by cairnward",
                          f"cairnward-egress: {decision.action.target} denied: {decision.reason}\n",
                          {"X-Cairnward-Decision": "deny"})  # fmt: skip

    def _inspection(self, session: Session, host: str, port: int) -> TlsInspection | None:
        """The environment's inspection settings, when this connection is to be inspected."""
        if session.trusted_core:
            return None  # the core's own calls: it checks and records them itself
        env = self.config.environments.get(session.environment)
        if env is None or not env.tls_inspection.enabled:
            return None
        tls = env.tls_inspection
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        if any(host_matches(target, p) or host_matches(host, p) for p in tls.skip_hosts):
            return None
        return tls

    def _tls_contexts(self, host: str) -> tuple[ssl.SSLContext, ssl.SSLContext]:
        from cairnward.egress import tls

        if self._issuer is None:
            self._issuer = tls.Issuer(self.config.paths.data_dir)
        if self._upstream_ssl is None:
            self._upstream_ssl = tls.upstream_context()
        return self._issuer.context(host), self._upstream_ssl

    async def _record_request(self, action: Action, ex: Exchange) -> None:
        """One audit record per inspected request; the path and query only as a digest."""
        req = Action(
            kind="net.request", target=action.target, environment=action.environment,
            client=action.client, run_id=action.run_id, actor=action.actor,
            details={"method": ex.method[:20], "status": ex.status, "bytes_up": ex.bytes_up,
                     "bytes_down": ex.bytes_down, "via": "egress", "inspected": True,
                     "rule": ex.verdict.rule, **({"error": ex.error} if ex.error else {})},
        )  # fmt: skip
        allowed = ex.verdict.allowed
        decision = Decision(req, "allow" if allowed else "deny", "low" if allowed else "medium",
                            ex.verdict.reason)  # fmt: skip
        await asyncio.to_thread(self.engine.audit_request, decision, digest(ex.target),
                                "ok" if ex.error is None else "error")  # fmt: skip

    async def _inspect(self, action: Action, inspection: TlsInspection, host: str, port: int,
                       reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       up_reader: asyncio.StreamReader, up_writer: asyncio.StreamWriter,
                       counts: dict[str, int]) -> str | None:  # fmt: skip
        """End the tool's TLS here, open our own to the host, check each request. Returns an
        error for the connection's audit record, or None."""
        try:
            server_ctx, client_ctx = await asyncio.to_thread(self._tls_contexts, host)
        except (OSError, ValueError) as exc:
            return f"inspection unavailable: {type(exc).__name__}"
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.wait_for(writer.start_tls(server_ctx), self.limits.connect_timeout_s)
        except (ssl.SSLError, ConnectionError, TimeoutError, OSError) as exc:
            return (
                f"the tool refused Cairnward's certificate ({type(exc).__name__}): it "
                "doesn't trust the local CA, or pins certificates (add it to skip_hosts)"
            )
        try:
            await asyncio.wait_for(up_writer.start_tls(client_ctx, server_hostname=host),
                                   self.limits.connect_timeout_s)  # fmt: skip
        except ssl.SSLCertVerificationError as exc:
            await self._reply(writer, 502, "Bad Gateway",
                              f"cairnward-egress: {host}'s certificate failed verification: "
                              f"{exc.verify_message}\n")  # fmt: skip
            return f"upstream certificate failed verification: {exc.verify_message}"
        except (ssl.SSLError, ConnectionError, TimeoutError, OSError) as exc:
            await self._reply(writer, 502, "Bad Gateway",
                              f"cairnward-egress: TLS to {host} failed\n")  # fmt: skip
            return f"upstream TLS failed: {type(exc).__name__}"

        async def record(ex: Exchange) -> None:
            counts["bytes_up"] += ex.bytes_up
            counts["bytes_down"] += ex.bytes_down
            counts["requests"] = counts.get("requests", 0) + 1
            if not ex.verdict.allowed:
                counts["denied"] = counts.get("denied", 0) + 1
            await self._record_request(action, ex)

        async def pipe(cr: asyncio.StreamReader, cw: asyncio.StreamWriter,
                       ur: asyncio.StreamReader, uw: asyncio.StreamWriter) -> None:  # fmt: skip
            await self._pipe(cr, cw, ur, uw, counts)

        inspector = Inspector(lambda m, p: check_request(inspection, host, port, m, p),
                              record, pipe, self.limits.idle_timeout_s)  # fmt: skip
        await inspector.run(reader, writer, up_reader, up_writer)
        return None

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
        if any(_CONTROL.search(line) for line in lines):
            # a bare LF or other control character could smuggle a second request or
            # header past us to the server
            raise _BadRequest("control characters in the request header")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            raise _BadRequest("malformed request line") from None
        if not method or " " in version or not version.startswith("HTTP/"):
            raise _BadRequest("malformed request line")
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep or not name.strip() or name != name.strip():
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
            path, host_header = None, ""
        else:
            try:
                url = urlsplit(target)
                url.port  # noqa: B018 (raises ValueError for a bad port or IPv6 literal)
            except ValueError:
                raise _BadRequest(f"malformed URL {target[:100]!r}") from None
            if url.scheme != "http" or not url.netloc:
                raise _BadRequest("only CONNECT and absolute http:// URLs are proxied")
            host, port = _split_hostport(url.netloc.rpartition("@")[2], 80)
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            name = f"[{host}]" if ":" in host else host
            host_header = name if port == 80 else f"{name}:{port}"

        display = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        action = Action(
            kind="net.connect", target=display, environment=session.environment,
            client=session.client, run_id=session.run_id, actor=session.actor,
            details={"method": method.upper(), "via": "egress"},
        )  # fmt: skip
        if session.trusted_core:
            decision = Decision(action, "allow", "low", "requested by the core (already checked)")
            if session.any_environment:
                action.details["environment"] = "checked by the core in its own environment"
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
        inspection = self._inspection(session, host, port)
        if inspection is not None and path is not None:
            # plain HTTP: the request is already readable, so the same rules apply to it
            verdict = check_request(inspection, host, port, method, request_path(path))
            if not verdict.allowed:
                await self._record_request(action, Exchange(method, path, verdict, status=403))
                await self._reply(writer, 403, "Forbidden by cairnward",
                                  f"cairnward-egress: {method} denied: {verdict.reason}\n",
                                  {"X-Cairnward-Decision": "deny"})  # fmt: skip
                return
            ok = Verdict(True, verdict.reason, verdict.rule)
            await self._record_request(action, Exchange(method, path, ok))

        # Resolve once, refuse special addresses, dial what was checked.
        try:
            addresses = await self._resolve(host, port)
        except (OSError, UnicodeError) as exc:  # UnicodeError: a label getaddrinfo can't encode
            reason = f"DNS lookup failed: {getattr(exc, 'strerror', None) or exc}"
            await self._deny(writer, Decision(action, "deny", decision.risk, reason))
            return
        chosen, refused = None, []
        for ip in addresses:
            why = special_reason(ip)
            if why is None or self._explicitly_allowed_ip(action, ip, port,
                                                          session.any_environment):  # fmt: skip
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
        details_extra: dict[str, Any] = {}
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
                if path is None and inspection is not None:
                    details_extra["inspected"] = True
                    error = await self._inspect(action, inspection, host, port, reader, writer,
                                                up_reader, up_writer, counts)  # fmt: skip
                    if error:
                        outcome = "error"
                elif path is None:
                    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    await writer.drain()
                    await self._pipe(reader, writer, up_reader, up_writer, counts)
                else:
                    # Host comes from the URL that was checked, never from the client: on a
                    # shared address, another Host would reach a site policy never saw.
                    out = [f"{method} {path} {version}", f"Host: {host_header}"]
                    out += [f"{k}: {v}" for k, v in headers
                            if k.lower() not in HOP_BY_HOP and k.lower() != "host"]  # fmt: skip
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
            **details_extra,
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
