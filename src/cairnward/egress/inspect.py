"""Request-level checks for traffic cairnward-egress decrypts (FR-NET-2, ADR-0018).

`Inspector.run` sits between two connections that are already TLS on both sides: the tool's
(ended with a certificate from Cairnward's CA) and the real host's (verified). It passes
HTTP/1.1 messages between them with h11, so framing (Content-Length, chunked, keep-alive, HEAD,
1xx) follows a tested implementation rather than ours. Each request is checked against the
environment's rules before it goes out: a denied one is answered 403 here and never reaches
the host. A protocol switch (WebSocket's 101) is checked as a request, then piped unread.

The audit gets one record per request: method, a digest of the path and query (they can hold
tokens), status, byte counts and the rule that decided. Never the path itself or any content.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache

import h11

from cairnward.config.models import HttpRule, TlsInspection
from cairnward.policy.engine import host_matches

MAX_HEADERS = 64 * 1024


@lru_cache(maxsize=512)
def _glob(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matches(path: str, pattern: str) -> bool:
    """'*' matches within one path segment, '**' across segments. '/a/**' also matches '/a'."""
    if pattern.endswith("/**") and path == pattern[:-3]:
        return True
    return _glob(pattern).match(path) is not None


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
# Characters a server may read as a path separator or terminator once decoded: a path that
# hides one behind an escape (or uses a backslash) can't be matched safely, so it's refused.
_AMBIGUOUS = re.compile(r"%(2f|5c|00)|\\", re.IGNORECASE)


def _decode_unreserved(m: re.Match[str]) -> str:
    ch = chr(int(m.group(1), 16))
    return ch if ch in _UNRESERVED else "%" + m.group(1).upper()


def normalize_path(path: str) -> str:
    """The path as a server would resolve it (RFC 3986 6.2.2): escapes of unreserved characters
    decoded (so `%2e` is a dot and `%61` is `a`), other escapes in upper case, repeated slashes
    collapsed, `.` and `..` segments removed. `..` never climbs above the root."""
    path = _ESCAPE.sub(_decode_unreserved, path)
    out: list[str] = []
    segments = path.split("/")
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg == "..":
            if out:
                out.pop()
            if last:
                out.append("")
        elif seg == ".":
            if last:
                out.append("")
        elif seg or last:
            out.append(seg)
    return "/" + "/".join(out) if out != [""] else "/"


def ambiguous_path(path: str) -> bool:
    """True when the raw path holds an encoded slash, backslash or NUL (see _AMBIGUOUS)."""
    return _AMBIGUOUS.search(path) is not None


def request_path(target: str) -> str:
    """The path of a request target, normalized (normalize_path), without the query (rules
    don't see the query). An ambiguous path (ambiguous_path) is returned raw, for
    check_request to refuse."""
    if "://" in target:  # absolute form inside a tunnel: drop scheme and authority
        target = "/" + target.split("://", 1)[1].partition("/")[2]
    path = target.split("?", 1)[0].split("#", 1)[0] or "/"
    if ambiguous_path(path):
        return path
    return normalize_path(path)


def host_header_name(value: str) -> str:
    """The host a Host header names: no port, brackets or trailing dot, lowercase."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        value = value.rsplit(":", 1)[0]
    return value.rstrip(".")


def fronted(headers: list[tuple[bytes, bytes]], target: str, tunnel_host: str) -> str | None:
    """Why a request names another site than the tunnel it came through, or None. On a shared
    CDN that would reach a site egress never checked (domain fronting)."""
    names = {host_header_name(v.decode("latin-1")) for k, v in headers if k.lower() == b"host"}
    if "://" in target:  # absolute form carries its own authority
        names.add(host_header_name(target.split("://", 1)[1].split("/", 1)[0].rpartition("@")[2]))
    wanted = tunnel_host.strip("[]").lower().rstrip(".")
    other = sorted(n for n in names if n != wanted)
    if other:
        return f"the request names {other[0]}, but the tunnel was opened to {wanted}"
    return None


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    rule: int | None  # index into rules, None for the `unmatched` default


def check_request(tls: TlsInspection, host: str, port: int, method: str, path: str) -> Verdict:
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    method = method.upper()
    if ambiguous_path(path):
        return Verdict(False, "the path holds an encoded slash, backslash or NUL", None)
    for i, rule in enumerate(tls.rules):
        if not (host_matches(target, rule.host) or host_matches(host, rule.host)):
            continue
        if rule.method not in ("*", method) or not path_matches(path, rule.path):
            continue
        return Verdict(rule.decision == "allow", _describe(rule, i), i)
    return Verdict(tls.unmatched == "allow", f"no rule matched (unmatched = {tls.unmatched})", None)


def _describe(rule: HttpRule, i: int) -> str:
    return f"rule {i + 1}: {rule.decision} {rule.method} {rule.host}{rule.path}"


@dataclass
class Exchange:
    """One request, for the audit."""

    method: str
    target: str  # path and query; digested before it's recorded
    verdict: Verdict
    status: int | None = None
    bytes_up: int = 0
    bytes_down: int = 0
    error: str | None = None


Record = Callable[[Exchange], Awaitable[None]]
Check = Callable[[str, str], Verdict]
Pipe = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter, asyncio.StreamReader, asyncio.StreamWriter],
    Awaitable[None],
]


class _Closed(Exception):
    pass


class MalformedRequest(Exception):
    """The request isn't valid HTTP/1.1 (e.g. two Host headers): answered 400, never sent on."""


async def _next(conn: h11.Connection, reader: asyncio.StreamReader, idle_s: float) -> object:
    while True:
        ev = conn.next_event()
        if ev is not h11.NEED_DATA:
            return ev
        data = await asyncio.wait_for(reader.read(65536), idle_s)
        conn.receive_data(data)  # b"" is end of stream


async def _send_event(conn: h11.Connection, writer: asyncio.StreamWriter, ev: h11.Event) -> None:
    data = conn.send(ev)
    if data:
        writer.write(data)
        await writer.drain()


# Headers about this hop only: never passed on (the proxy token above all).
HOP_HEADERS = {b"proxy-authorization", b"proxy-connection", b"connection", b"keep-alive",
               b"host", b"expect", b"te", b"trailer", b"upgrade"}  # fmt: skip


@dataclass
class Forwarded:
    status: int | None = None
    bytes_up: int = 0
    bytes_down: int = 0


async def forward_once(head: bytes, c_reader: asyncio.StreamReader,
                       c_writer: asyncio.StreamWriter, u_reader: asyncio.StreamReader,
                       u_writer: asyncio.StreamWriter, *, target: str, host_header: str,
                       idle_s: float) -> Forwarded:  # fmt: skip
    """Pass exactly one plain-HTTP request and its response, then end (I28).

    The request's own body is forwarded and nothing after it: a second request pipelined on the
    connection would otherwise reach the host without its own policy check. The response says
    `Connection: close`, so a keep-alive client opens a new connection, and is checked again,
    for its next request. `Host` comes from the URL that was checked, never from the client."""
    out = Forwarded()
    client = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEADERS)
    upstream = h11.Connection(h11.CLIENT, max_incomplete_event_size=MAX_HEADERS)
    client.receive_data(head)
    try:
        req = await _next(client, c_reader, idle_s)
    except h11.RemoteProtocolError as exc:
        raise MalformedRequest(str(exc)) from None
    if not isinstance(req, h11.Request):
        raise MalformedRequest("no request")
    headers = [(b"host", host_header.encode())]
    headers += [(k, v) for k, v in req.headers if k.lower() not in HOP_HEADERS]
    headers.append((b"connection", b"close"))
    if client.they_are_waiting_for_100_continue:
        await _send_event(client, c_writer, h11.InformationalResponse(status_code=100,
                                                                      headers=[]))  # fmt: skip
    await _send_event(upstream, u_writer, h11.Request(method=req.method, target=target.encode(),
                                                      headers=headers))  # fmt: skip
    out.bytes_up += len(head)
    while True:
        ev = await _next(client, c_reader, idle_s)
        if isinstance(ev, h11.Data):
            out.bytes_up += len(ev.data)
            await _send_event(upstream, u_writer, ev)
        elif isinstance(ev, h11.EndOfMessage):
            await _send_event(upstream, u_writer, ev)
            break
        else:
            raise _Closed
    while True:
        ev = await _next(upstream, u_reader, idle_s)
        if isinstance(ev, h11.InformationalResponse):
            continue  # 100 was answered here; nothing else asked to switch protocols
        if isinstance(ev, h11.Response):
            out.status = ev.status_code
            hop = (b"connection", b"keep-alive", b"proxy-connection")
            kept = [(k, v) for k, v in ev.headers if k.lower() not in hop]
            await _send_event(client, c_writer, h11.Response(
                status_code=ev.status_code, reason=ev.reason,
                headers=[*kept, (b"connection", b"close")]))  # fmt: skip
        elif isinstance(ev, h11.Data):
            out.bytes_down += len(ev.data)
            await _send_event(client, c_writer, ev)
        elif isinstance(ev, h11.EndOfMessage):
            await _send_event(client, c_writer, ev)
            return out
        else:
            raise _Closed


class Inspector:
    def __init__(self, check: Check, record: Record, pipe: Pipe, idle_s: float,
                 tunnel_host: str | None = None):  # fmt: skip
        self.tunnel_host = tunnel_host
        self.check = check
        self.record = record
        self.pipe = pipe
        self.idle_s = idle_s

    async def _next(self, conn: h11.Connection, reader: asyncio.StreamReader) -> object:
        return await _next(conn, reader, self.idle_s)

    _send = staticmethod(_send_event)

    async def _refuse(
        self,
        client: h11.Connection,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        status: int,
        text: str,
    ) -> None:
        """Answer the tool ourselves, after reading the rest of its request."""
        while True:
            ev = await self._next(client, c_reader)
            if isinstance(ev, h11.EndOfMessage | h11.ConnectionClosed) or ev is h11.PAUSED:
                break
        body = text.encode()
        await self._send(
            client,
            c_writer,
            h11.Response(
                status_code=status,
                headers=[
                    ("content-type", "text/plain; charset=utf-8"),
                    ("content-length", str(len(body))),
                    ("x-cairnward-decision", "deny"),
                    ("connection", "close"),
                ],
            ),
        )
        await self._send(client, c_writer, h11.Data(data=body))
        await self._send(client, c_writer, h11.EndOfMessage())

    async def run(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
    ) -> None:
        client = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEADERS)
        upstream = h11.Connection(h11.CLIENT, max_incomplete_event_size=MAX_HEADERS)
        while True:
            try:
                ev = await self._next(client, c_reader)
            except (h11.RemoteProtocolError, TimeoutError, ConnectionError):
                return
            if isinstance(ev, h11.ConnectionClosed) or not isinstance(ev, h11.Request):
                return
            method = ev.method.decode("ascii", "replace")
            target = ev.target.decode("latin-1")
            ex = Exchange(method, target, self.check(method, request_path(target)))
            if method.upper() == "CONNECT":
                ex.verdict = Verdict(False, "a tunnel inside an inspected tunnel", None)
            elif self.tunnel_host and (why := fronted(list(ev.headers), target,
                                                      self.tunnel_host)):  # fmt: skip
                ex.verdict = Verdict(False, why, None)
            if not ex.verdict.allowed:
                ex.status = 403
                try:
                    await self._refuse(
                        client,
                        c_reader,
                        c_writer,
                        403,
                        f"cairnward-egress: {method} denied: {ex.verdict.reason}\n",
                    )
                except (
                    h11.LocalProtocolError,
                    h11.RemoteProtocolError,
                    TimeoutError,
                    ConnectionError,
                ) as exc:
                    ex.error = type(exc).__name__
                await self.record(ex)
                return  # we said Connection: close
            try:
                switched = await self._exchange(
                    ev, ex, client, upstream, c_reader, c_writer, u_reader, u_writer
                )
            except (
                h11.LocalProtocolError,
                h11.RemoteProtocolError,
                TimeoutError,
                ConnectionError,
                _Closed,
            ) as exc:
                ex.error = type(exc).__name__
                await self.record(ex)
                return
            await self.record(ex)
            if switched:
                await self._switch(client, upstream, c_reader, c_writer, u_reader, u_writer)
                return
            if h11.MUST_CLOSE in (
                client.our_state,
                client.their_state,
                upstream.our_state,
                upstream.their_state,
            ):
                return
            try:
                client.start_next_cycle()
                upstream.start_next_cycle()
            except h11.LocalProtocolError:
                return

    async def _exchange(
        self,
        req: h11.Request,
        ex: Exchange,
        client: h11.Connection,
        upstream: h11.Connection,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
    ) -> bool:
        """Forward one request and its response. True when the protocol switched (101)."""
        if client.they_are_waiting_for_100_continue:
            # Answer 100 ourselves and don't pass Expect on, so the body can stream straight
            # through without waiting for the host.
            await self._send(
                client, c_writer, h11.InformationalResponse(status_code=100, headers=[])
            )
        headers = [(k, v) for k, v in req.headers if k.lower() != b"expect"]
        await self._send(
            upstream,
            u_writer,
            h11.Request(
                method=req.method, target=req.target, headers=headers, http_version=req.http_version
            ),
        )
        while True:
            ev = await self._next(client, c_reader)
            if isinstance(ev, h11.Data):
                ex.bytes_up += len(ev.data)
                await self._send(upstream, u_writer, ev)
            elif isinstance(ev, h11.EndOfMessage):
                await self._send(upstream, u_writer, ev)
                break
            elif ev is h11.PAUSED:  # an Upgrade request with no body
                break
            else:
                raise _Closed
        while True:
            ev = await self._next(upstream, u_reader)
            if isinstance(ev, h11.InformationalResponse):
                if ev.status_code == 100:
                    continue  # already sent our own
                await self._send(client, c_writer, ev)
                if ev.status_code == 101:
                    ex.status = 101
                    return True
            elif isinstance(ev, h11.Response):
                ex.status = ev.status_code
                await self._send(client, c_writer, ev)
            elif isinstance(ev, h11.Data):
                ex.bytes_down += len(ev.data)
                await self._send(client, c_writer, ev)
            elif isinstance(ev, h11.EndOfMessage):
                await self._send(client, c_writer, ev)
                return False
            else:
                raise _Closed

    async def _switch(
        self,
        client: h11.Connection,
        upstream: h11.Connection,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
    ) -> None:
        """After a 101: send what h11 already read on, then pipe the rest unread."""
        up_extra, _ = client.trailing_data
        down_extra, _ = upstream.trailing_data
        if up_extra:
            u_writer.write(up_extra)
        if down_extra:
            c_writer.write(down_extra)
        await self.pipe(c_reader, c_writer, u_reader, u_writer)
