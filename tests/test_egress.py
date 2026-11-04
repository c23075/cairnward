import asyncio
import base64
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.egress import EgressProxy, Session, Sessions, special_reason
from cairnward.egress.proxy import _BadRequest, _split_hostport
from cairnward.policy import (
    ApprovalAnswer,
    ApprovalRequest,
    PolicyEngine,
    ScriptedApprover,
)

# ------------------------------------------------------------------ fixtures: fake origins


class Origin(BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []  # noqa: RUF012

    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        Origin.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = b"hello from origin"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def origin() -> Iterator[int]:
    Origin.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def echo() -> Iterator[int]:
    srv = socket.create_server(("127.0.0.1", 0))

    def run() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                while data := conn.recv(4096):
                    conn.sendall(data)

    threading.Thread(target=run, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ fixtures: proxy


class Running:
    def __init__(self, proxy: EgressProxy, token: str, log: AuditLog, approver: ScriptedApprover):
        self.proxy, self.token, self.log, self.approver = proxy, token, log, approver

    @property
    def auth(self) -> str:
        return "Basic " + base64.b64encode(f"cw:{self.token}".encode()).decode()

    def raw(self, request: bytes, *, then: bytes | None = None) -> bytes:
        with socket.create_connection(("127.0.0.1", self.proxy.port), timeout=10) as s:
            s.sendall(request)
            if then is not None:
                head = s.recv(4096)
                s.sendall(then)
                time.sleep(0.2)
                s.shutdown(socket.SHUT_WR)
                return head + _recv_all(s)
            return _recv_all(s)

    def get(self, url: str, auth: bool = True) -> bytes:
        hdr = f"Proxy-Authorization: {self.auth}\r\n" if auth else ""
        host = url.split("/")[2]
        return self.raw(f"GET {url} HTTP/1.1\r\nHost: {host}\r\n{hdr}\r\n".encode())


def wait_records(log: AuditLog, n: int, timeout: float = 5) -> list[Any]:
    """The post record is written after the connection closes; wait for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        recs = log.records()
        if len(recs) >= n:
            return recs
        time.sleep(0.02)
    raise AssertionError(f"expected {n} audit records, got {len(log.records())}")


def _recv_all(s: socket.socket) -> bytes:
    out = b""
    while chunk := s.recv(65536):
        out += chunk
    return out


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Callable[..., Running]]:
    loops: list[tuple[asyncio.AbstractEventLoop, EgressProxy, AuditLog]] = []

    def _start(
        privacy: str,
        *,
        answer: Callable[[ApprovalRequest], ApprovalAnswer] | None = None,
        resolver: Callable[[str, int], list[str]] | None = None,
        egress: str = "",
    ) -> Running:
        paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
        (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
        (paths.config_dir / "environments" / "e.toml").write_text(f"[privacy]\n{privacy}\n")
        (paths.config_dir / "cairnward.toml").write_text(f"[egress]\n{egress}\n")
        cfg = load_config(paths)
        log = AuditLog.open(paths)
        approver = ScriptedApprover(answer or (lambda r: ApprovalAnswer(False)))
        engine = PolicyEngine(cfg, approver=approver, audit=log)
        sessions = Sessions()
        token = sessions.create(Session(environment="e", client="test", actor="agent:test"))
        proxy = EgressProxy(cfg, engine, sessions, port=0, resolver=resolver)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(proxy.start())
        threading.Thread(target=loop.run_forever, daemon=True).start()
        loops.append((loop, proxy, log))
        return Running(proxy, token, log, approver)

    yield _start
    for loop, proxy, log in loops:
        asyncio.run_coroutine_threadsafe(proxy.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        log.close()


def allow_local(port: int) -> str:
    return f'network = "allowlist"\nnetwork_allow_hosts = ["127.0.0.1:{port}"]'


# ------------------------------------------------------------------ tests


def test_requires_session_token(start: Callable[..., Running], origin: int) -> None:
    r = start(allow_local(origin))
    resp = r.get(f"http://127.0.0.1:{origin}/", auth=False)
    assert resp.startswith(b"HTTP/1.1 407") and b"Proxy-Authenticate" in resp


def test_http_forwarded_and_audited(start: Callable[..., Running], origin: int) -> None:
    r = start(allow_local(origin))
    resp = r.get(f"http://127.0.0.1:{origin}/path?q=1")
    assert resp.startswith(b"HTTP/1.0 200") or resp.startswith(b"HTTP/1.1 200")
    assert resp.endswith(b"hello from origin")
    assert "proxy-authorization" not in Origin.seen[0]  # the token never leaves the machine
    pre, post = wait_records(r.log, 2)
    assert (pre.action, pre.phase, pre.decision, pre.client) == (
        "net.connect",
        "pre",
        "allow",
        "test",
    )
    assert post.outcome == "ok" and post.details["bytes_down"] > 0 and post.details["bytes_up"] > 0
    assert r.log.verify().ok


def test_connect_tunnel(start: Callable[..., Running], echo: int) -> None:
    r = start(allow_local(echo))
    req = f"CONNECT 127.0.0.1:{echo} HTTP/1.1\r\nProxy-Authorization: {r.auth}\r\n\r\n".encode()
    resp = r.raw(req, then=b"ping through the tunnel")
    assert resp.startswith(b"HTTP/1.1 200 Connection established")
    assert resp.endswith(b"ping through the tunnel")
    post = wait_records(r.log, 2)[-1]
    assert post.details["bytes_up"] == len(b"ping through the tunnel")


def test_network_off_denies_and_records(start: Callable[..., Running], origin: int) -> None:
    r = start('network = "off"')
    resp = r.get(f"http://127.0.0.1:{origin}/")
    assert resp.startswith(b"HTTP/1.1 403") and b"X-Cairnward-Decision: deny" in resp
    assert b"network = off" in resp
    (rec,) = wait_records(r.log, 1)
    assert (rec.phase, rec.decision) == ("event", "deny")
    assert Origin.seen == []


@pytest.mark.parametrize(
    ("resolved", "why"),
    [
        (["127.0.0.1"], b"loopback"),
        (["169.254.169.254"], b"link-local"),
        (["::ffff:127.0.0.1"], b"loopback"),
        (["100.100.100.200"], b"metadata"),
    ],
)
def test_dns_rebinding_refused(
    start: Callable[..., Running], resolved: list[str], why: bytes
) -> None:
    """An allowed name that resolves to this machine or metadata services is refused."""
    r = start('network = "allowlist"\nnetwork_allow_hosts = ["*.example.org"]',
              resolver=lambda host, port: resolved)  # fmt: skip
    resp = r.get("http://api.example.org/")
    assert resp.startswith(b"HTTP/1.1 403") and why in resp


def test_localhost_by_name_refused_even_in_open_mode(
    start: Callable[..., Running], origin: int
) -> None:
    r = start('preset = "open"')
    assert r.get(f"http://localhost:{origin}/").startswith(b"HTTP/1.1 403")
    assert Origin.seen == []


def test_ask_approved_and_refused(start: Callable[..., Running], origin: int) -> None:
    host = f"127.0.0.1:{origin}"
    r = start('network = "ask"', answer=lambda req: ApprovalAnswer(req.target == host))
    # IP literal still has to be explicitly allowed after approval: loopback guard.
    resp = r.get(f"http://{host}/")
    assert resp.startswith(b"HTTP/1.1 403") and b"loopback" in resp
    assert r.approver.asked and r.approver.asked[0].target == host
    resp = r.get("http://10.9.9.9:1/")
    assert resp.startswith(b"HTTP/1.1 403") and b"not approved" in resp


def test_run_approval_covers_later_connections(start: Callable[..., Running], echo: int) -> None:
    """A "yes" for a run (from the sandbox, or asked here) isn't asked again in that run."""
    target = f"127.0.0.1:{echo}"
    r = start('network = "ask"\nnetwork_allow_hosts = ["127.0.0.1"]',
              answer=lambda req: ApprovalAnswer(True))  # fmt: skip
    r.proxy.sessions._by_token[r.token] = Session("e", "test", "run-9", "agent:test")
    req = f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: {r.auth}\r\n\r\n".encode()
    r.proxy.sessions.approve("run-9", target)
    assert r.raw(req, then=b"x").startswith(b"HTTP/1.1 200")
    assert r.approver.asked == []
    pre = wait_records(r.log, 1)[0]
    assert pre.details["reason"] == "approved for this run when the sandbox asked"


def test_approval_timeout_denies(start: Callable[..., Running]) -> None:
    def slow(req: ApprovalRequest) -> ApprovalAnswer:
        time.sleep(3)
        return ApprovalAnswer(True)

    r = start('network = "ask"', answer=slow, egress="approval_timeout_s = 1")
    resp = r.get("http://10.9.9.9:1/")
    assert resp.startswith(b"HTTP/1.1 403") and b"approval timed out" in resp


def test_host_header_comes_from_the_checked_url(start: Callable[..., Running], origin: int) -> None:
    """On a shared address another Host would reach a site the policy never checked."""
    r = start(allow_local(origin))
    req = (f"GET http://127.0.0.1:{origin}/ HTTP/1.1\r\nHost: not-allowed.example\r\n"
           f"host: second.example\r\nProxy-Authorization: {r.auth}\r\n\r\n")  # fmt: skip
    assert r.raw(req.encode()).endswith(b"hello from origin")
    assert Origin.seen[0]["host"] == f"127.0.0.1:{origin}"


def test_late_approval_is_dropped_and_prompts_dont_overlap(start: Callable[..., Running]) -> None:
    """After a timeout the prompt may still be open: a later "yes, this environment" must
    not be remembered, and no second prompt opens next to it."""
    release, open_now, most = threading.Event(), [0], [0]

    def held(req: ApprovalRequest) -> ApprovalAnswer:
        open_now[0] += 1
        most[0] = max(most[0], open_now[0])
        release.wait(10)
        open_now[0] -= 1
        return ApprovalAnswer(True, "environment")

    r = start('network = "ask"', answer=held, egress="approval_timeout_s = 1")
    first = r.get("http://10.9.9.9:1/")
    assert first.startswith(b"HTTP/1.1 403") and b"approval timed out" in first
    second = r.get("http://10.9.9.8:1/")  # the first prompt is still open
    assert second.startswith(b"HTTP/1.1 403") and b"still open" in second
    release.set()
    deadline = time.monotonic() + 5
    while open_now[0] and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)
    assert most[0] == 1 and len(r.approver.asked) == 1
    assert not r.proxy.engine.memory._grants  # the late "yes" wasn't kept


def test_unreachable_upstream(start: Callable[..., Running]) -> None:
    port = closed_port()
    r = start(allow_local(port))
    assert r.get(f"http://127.0.0.1:{port}/").startswith(b"HTTP/1.1 502")
    post = wait_records(r.log, 2)[-1]
    assert post.outcome == "error" and "connect failed" in post.details["error"]


@pytest.mark.parametrize(
    "request_line",
    [b"GET / HTTP/1.1", b"GET https://example.org/ HTTP/1.1", b"NONSENSE"],
)
def test_bad_requests(start: Callable[..., Running], request_line: bytes) -> None:
    r = start('preset = "open"')
    resp = r.raw(request_line + f"\r\nProxy-Authorization: {r.auth}\r\n\r\n".encode())
    assert resp.startswith(b"HTTP/1.1 400")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("example.org:443", ("example.org", 443)),
        ("Example.org", ("example.org", 80)),
        ("[::1]:8080", ("::1", 8080)),
        ("[2001:db8::1]", ("2001:db8::1", 80)),
    ],
)
def test_split_hostport(value: str, expected: tuple[str, int]) -> None:
    assert _split_hostport(value, 80) == expected


@pytest.mark.parametrize("value", [":443", "host:99999", "host:abc"])
def test_split_hostport_rejects(value: str) -> None:
    with pytest.raises(_BadRequest):
        _split_hostport(value, 80)


@pytest.mark.parametrize(
    ("ip", "special"),
    [
        ("8.8.8.8", False),
        ("10.0.0.5", False),
        ("192.168.1.2", False),
        ("127.0.0.53", True),
        ("::1", True),
        ("0.0.0.0", True),  # noqa: S104 - an address under test, not a bind
        ("169.254.169.254", True),
        ("fe80::1", True),
        ("224.0.0.1", True),
        ("::ffff:169.254.169.254", True),
        ("64:ff9b::7f00:1", True),
        ("2002:7f00:1::", True),
        ("168.63.129.16", True),
    ],
)
def test_special_addresses(ip: str, special: bool) -> None:
    assert (special_reason(ip) is not None) is special


def test_isolated_core_session_and_listed_local_addresses(
    start: Callable[..., Running], origin: int
) -> None:
    """The isolated core's one session: its connections were checked in their own
    environment, so an address on this machine is dialled when *some* environment lists
    it exactly; a core session for one environment (single process) only uses that
    environment's list."""
    r = start(allow_local(origin))  # listed in environment "e" only
    shared = r.proxy.sessions.create(Session(environment="coding", trusted_core=True,
                                             any_environment=True))  # fmt: skip
    per_env = r.proxy.sessions.create(Session(environment="coding", trusted_core=True))
    for token, expect in ((shared, b"HTTP/1.0 200"), (per_env, b"HTTP/1.1 403")):
        r.token = token
        assert r.get(f"http://127.0.0.1:{origin}/").startswith(expect)
    r.token = shared
    assert r.get(f"http://127.0.0.1:{origin + 1}/").startswith(b"HTTP/1.1 403")  # unlisted
    recs = wait_records(r.log, 3)
    assert any(x.details.get("environment") == "checked by the core in its own environment"
               for x in recs)  # fmt: skip
