"""TLS inspection in cairnward-egress (FR-NET-2): real TLS on both sides, real clients (httpx,
and curl when it's installed), a real HTTPS origin on this machine. No mocks of TLS or HTTP."""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.models import HttpRule, TlsInspection
from cairnward.config.paths import Paths
from cairnward.egress import EgressProxy, Session, Sessions
from cairnward.egress import tls as cwtls
from cairnward.egress.inspect import check_request, fronted, path_matches, request_path
from cairnward.policy import ApprovalAnswer, PolicyEngine, ScriptedApprover

# ------------------------------------------------------------------ an HTTPS origin


def _origin_cert(folder: Path) -> tuple[Path, Path]:
    """A throwaway CA and a certificate for 127.0.0.1 signed by it: (ca.pem, server.pem)."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test origin CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(hours=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False), True
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(hours=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = folder / "origin-ca.pem"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    server_pem = folder / "origin-server.pem"
    server_pem.write_bytes(
        leaf.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, server_pem


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so two requests can share a connection
    seen: list[tuple[str, str, bytes]] = []  # noqa: RUF012

    def log_message(self, *a: Any) -> None:
        pass

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = b""
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _answer(self, body: bytes, status: int = 200) -> None:
        Handler.seen.append((self.command, self.path, self._body()))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/ws":  # a protocol switch, then echo raw bytes
            Handler.seen.append(("GET", "/ws", b""))
            self.send_response(101)
            self.send_header("Upgrade", "echo")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.flush()
            while data := self.connection.recv(4096):
                self.connection.sendall(data.upper())
            self.close_connection = True
            return
        self._answer(b"secret page " + self.path.encode())

    def do_HEAD(self) -> None:
        self._answer(b"x" * 10)

    def do_POST(self) -> None:
        self._answer(b"posted")

    def do_DELETE(self) -> None:
        self._answer(b"deleted")


@pytest.fixture
def https_origin(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    Handler.seen = []
    ca_pem, server_pem = _origin_cert(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(server_pem)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], ca_pem
    srv.shutdown()


# ------------------------------------------------------------------ the proxy


class Proxy:
    def __init__(self, proxy: EgressProxy, sessions: Sessions, log: AuditLog, paths: Paths):
        self.proxy, self.sessions, self.log, self.paths = proxy, sessions, log, paths
        # a `cairnward run` agent that was given the CA
        self.token = sessions.create(
            Session(environment="e", client="test", actor="agent:t", inspect=True)
        )

    def url(self, token: str | None = None) -> str:
        return f"http://cw:{token or self.token}@127.0.0.1:{self.proxy.port}"

    def bundle(self) -> Path:
        return cwtls.trust_bundle(self.paths.data_dir, self.paths.state_dir / "bundle.pem")

    def client(self, verify: Path | bool | None = None, token: str | None = None) -> httpx.Client:
        v: Any = verify if verify is not None else str(self.bundle())
        if isinstance(v, Path):
            v = str(v)
        return httpx.Client(proxy=self.url(token), verify=v, timeout=10)

    def records(self, action: str | None = None, wait_for: int = 0) -> list[Any]:
        deadline = dt.datetime.now() + dt.timedelta(seconds=5)
        while True:
            recs = [r for r in self.log.records() if action is None or r.action == action]
            if len(recs) >= wait_for or dt.datetime.now() > deadline:
                return recs
            threading.Event().wait(0.02)


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Callable[..., Proxy]]:
    running: list[tuple[asyncio.AbstractEventLoop, EgressProxy, AuditLog]] = []

    def _start(port: int, origin_ca: Path | None, inspection: str) -> Proxy:
        paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
        (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        (paths.config_dir / "environments" / "e.toml").write_text(
            f'[privacy]\nnetwork = "allowlist"\nnetwork_allow_hosts = ["127.0.0.1:{port}"]\n'
            f"\n[tls_inspection]\n{inspection}\n"
        )
        cfg = load_config(paths)
        log = AuditLog.open(paths)
        engine = PolicyEngine(
            cfg, approver=ScriptedApprover(lambda r: ApprovalAnswer(False)), audit=log
        )
        upstream = None
        if origin_ca is not None:  # how egress checks the origin: its CA, as for a real site
            upstream = cwtls.upstream_context()
            upstream.load_verify_locations(origin_ca)
        sessions = Sessions()
        proxy = EgressProxy(cfg, engine, sessions, port=0, upstream_ssl=upstream)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(proxy.start())
        threading.Thread(target=loop.run_forever, daemon=True).start()
        running.append((loop, proxy, log))
        return Proxy(proxy, sessions, log, paths)

    yield _start
    for loop, proxy, log in running:
        asyncio.run_coroutine_threadsafe(proxy.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        log.close()


RULES = """enabled = true
unmatched = "deny"
[[tls_inspection.rules]]
host = "127.0.0.1"
method = "DELETE"
path = "/repos/**"
decision = "deny"
[[tls_inspection.rules]]
host = "127.0.0.1"
path = "/repos/**"
decision = "allow"
[[tls_inspection.rules]]
host = "127.0.0.1"
method = "HEAD"
path = "/**"
decision = "allow"
"""


# ------------------------------------------------------------------ requests through it


def test_allowed_request_reaches_the_origin_and_is_audited_as_a_digest(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    port, ca = https_origin
    p = start(port, ca, RULES)
    with p.client() as c:
        r = c.get(f"https://127.0.0.1:{port}/repos/me/x?token=abc")
    assert r.status_code == 200 and r.text == "secret page /repos/me/x?token=abc", r.text
    req = p.records("net.request", 1)[0]
    assert (req.decision, req.outcome, req.details["status"]) == ("allow", "ok", 200)
    assert req.details["method"] == "GET" and req.details["rule"] == 1
    assert req.params_digest and "token" not in json.dumps(req.details)
    assert "/repos/me" not in json.dumps([(r.target, r.details) for r in p.log.records()])
    post = next(r for r in p.records("net.connect", 2) if r.phase == "post")
    assert post.details["inspected"] is True and post.details["requests"] == 1
    assert p.log.verify().ok


def test_denied_requests_never_reach_the_origin(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    port, ca = https_origin
    p = start(port, ca, RULES)
    with p.client() as c:
        deleted = c.delete(f"https://127.0.0.1:{port}/repos/me/x")
        other = c.get(f"https://127.0.0.1:{port}/admin")
    assert deleted.status_code == 403 and deleted.headers["x-cairnward-decision"] == "deny"
    assert "rule 1" in deleted.text
    assert other.status_code == 403 and "unmatched = deny" in other.text
    assert Handler.seen == []
    denies = [r for r in p.records("net.request", 2) if r.decision == "deny"]
    assert len(denies) == 2


def test_keep_alive_bodies_and_head(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    port, ca = https_origin
    p = start(port, ca, RULES)
    with p.client() as c:
        a = c.post(f"https://127.0.0.1:{port}/repos/a", content=b"x" * 100_000)
        b = c.post(f"https://127.0.0.1:{port}/repos/b", content=iter([b"chunk-1 ", b"chunk-2"]))
        h = c.head(f"https://127.0.0.1:{port}/anything")
    assert (a.text, b.text, h.status_code, h.content) == ("posted", "posted", 200, b"")
    assert Handler.seen[0][2] == b"x" * 100_000 and Handler.seen[1][2] == b"chunk-1 chunk-2"
    connects = [r for r in p.records("net.connect", 2) if r.phase == "post"]
    assert len(connects) == 1 and connects[0].details["requests"] == 3  # one connection


def test_protocol_switch_is_checked_then_piped(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    port, ca = https_origin
    p = start(port, ca, 'enabled = true\nunmatched = "allow"')
    ctx = ssl.create_default_context(cafile=str(p.bundle()))
    with socket.create_connection(("127.0.0.1", p.proxy.port), timeout=10) as raw:
        raw.sendall(
            f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nProxy-Authorization: Basic "
            f"{__import__('base64').b64encode(f'cw:{p.token}'.encode()).decode()}\r\n\r\n".encode()
        )
        assert raw.recv(4096).startswith(b"HTTP/1.1 200")
        with ctx.wrap_socket(raw, server_hostname="127.0.0.1") as s:
            s.sendall(
                b"GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Upgrade: echo\r\nConnection: Upgrade\r\n\r\n"
            )
            head = b""
            while b"\r\n\r\n" not in head:
                head += s.recv(4096)
            assert head.startswith(b"HTTP/1.1 101")
            s.sendall(b"hello")
            assert s.recv(4096) == b"HELLO"
    req = p.records("net.request", 1)[0]
    assert req.details["status"] == 101


def test_tool_without_the_ca_fails_closed(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    port, ca = https_origin
    p = start(port, ca, RULES)
    with p.client(verify=ca) as c, pytest.raises(httpx.ConnectError):
        c.get(f"https://127.0.0.1:{port}/repos/x")
    assert Handler.seen == []
    post = next(r for r in p.records("net.connect", 2) if r.phase == "post")
    assert post.outcome == "error" and "refused Cairnward's certificate" in post.details["error"]


def test_origin_certificate_is_verified(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    port, _ = https_origin
    p = start(port, None, RULES)  # egress trusts only the public roots: the origin's CA isn't one
    with p.client() as c:
        r = c.get(f"https://127.0.0.1:{port}/repos/x")
    assert r.status_code == 502 and "failed verification" in r.text
    assert Handler.seen == []
    post = next(r for r in p.records("net.connect", 2) if r.phase == "post")
    assert "upstream certificate failed verification" in post.details["error"]


def test_skip_hosts_and_the_core_are_tunnelled_unread(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    port, ca = https_origin
    p = start(port, ca, f'enabled = true\nunmatched = "deny"\nskip_hosts = ["127.0.0.1:{port}"]')
    with p.client(verify=ca) as c:  # the origin's own certificate: nothing in between
        assert c.get(f"https://127.0.0.1:{port}/x").status_code == 200

    q = start(port, ca, 'enabled = true\nunmatched = "deny"')
    core = q.sessions.create(Session(environment="e", trusted_core=True))
    with q.client(verify=ca, token=core) as c:
        assert c.get(f"https://127.0.0.1:{port}/y").status_code == 200
    assert p.records("net.request") == [] and q.records("net.request") == []


def test_programs_without_the_ca_are_tunnelled_even_in_an_inspecting_environment(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    """MCP servers, restic, `mcp login`, plugin commands and downloads use egress in the same
    environment but are never given the CA: decrypting their HTTPS would break it."""
    port, ca = https_origin
    p = start(port, ca, 'enabled = true\nunmatched = "deny"')
    for client in ("mcp:files", "restic", "cairnward-download"):
        token = p.sessions.create(Session(environment="e", client=client))
        with p.client(verify=ca, token=token) as c:
            assert c.get(f"https://127.0.0.1:{port}/x").status_code == 200, client
    assert p.records("net.request") == []


def test_only_runs_given_the_ca_are_inspected(tmp_path: Path) -> None:
    """/v1/runs also serves restic, `mcp login` and plugin commands: only `cairnward run` says it
    gave the program the CA, and only an inspecting environment honours it."""
    import base64

    from cairnward.core.management import management_app
    from cairnward.egress.control import LocalEgress
    from cairnward.gateway.clients import RunKeys

    paths = Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s")
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "on.toml").write_text("[tls_inspection]\nenabled = true")
    (paths.config_dir / "environments" / "off.toml").write_text("")
    sessions = Sessions()
    app = management_app(load_config(paths), audit=None, run_keys=RunKeys(),
                         egress=LocalEgress(sessions), isolated=False)  # fmt: skip
    body = {"client": "x", "profile": "generic", "workspace": "/w", "program": "p",
            "arg_count": 0, "sandbox": "srt"}  # fmt: skip

    def inspected(**extra: Any) -> bool:
        from fastapi.testclient import TestClient

        with TestClient(app) as c:
            token = c.post("/v1/runs", json={**body, **extra}).json()["egress_token"]
        auth = "Basic " + base64.b64encode(f"cw:{token}".encode()).decode()
        session = sessions.lookup(auth)
        assert session is not None
        return session.inspect

    assert inspected(environment="on", tls_trust=True)
    assert not inspected(environment="on")  # a helper program (restic, mcp login, a plugin)
    assert not inspected(environment="off", tls_trust=True)


def test_isolated_mode_passes_the_flag_to_egress() -> None:
    from fastapi.testclient import TestClient

    from cairnward.egress.control import control_app

    sessions = Sessions()
    with TestClient(control_app(sessions)) as c:
        token = c.post("/v1/sessions", json={"environment": "e", "inspect": True}).json()["token"]
    assert sessions._by_token[token].inspect is True


def test_a_request_for_another_site_is_refused(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    """Domain fronting: a tunnel to an allowed host, a Host header for another site. On a shared
    CDN that request would reach a site egress never checked."""
    import base64

    port, ca = https_origin
    p = start(port, ca, 'enabled = true\nunmatched = "allow"')
    ctx = ssl.create_default_context(cafile=str(p.bundle()))
    auth = base64.b64encode(f"cw:{p.token}".encode()).decode()
    with socket.create_connection(("127.0.0.1", p.proxy.port), timeout=10) as raw:
        raw.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nProxy-Authorization: Basic "
                    f"{auth}\r\n\r\n".encode())  # fmt: skip
        assert raw.recv(4096).startswith(b"HTTP/1.1 200")
        with ctx.wrap_socket(raw, server_hostname="127.0.0.1") as s:
            s.sendall(b"GET /x HTTP/1.1\r\nHost: blocked.example\r\n\r\n")
            answer = b""
            while chunk := s.recv(4096):
                answer += chunk
    assert answer.startswith(b"HTTP/1.1 403") and b"blocked.example" in answer
    assert Handler.seen == []
    assert p.records("net.request", 1)[0].decision == "deny"


@pytest.mark.parametrize(
    ("headers", "target", "refused"),
    [
        ([(b"host", b"api.example.org")], "/x", False),
        ([(b"host", b"API.example.org.:443")], "/x", False),
        ([(b"host", b"other.example.org")], "/x", True),
        ([(b"host", b"api.example.org")], "https://other.example.org/x", True),
        ([(b"host", b"api.example.org"), (b"host", b"other.example.org")], "/x", True),
    ],
)
def test_fronting_check(headers: list[tuple[bytes, bytes]], target: str, refused: bool) -> None:
    assert (fronted(headers, target, "api.example.org") is not None) is refused


def test_off_by_default(start: Callable[..., Proxy], https_origin: tuple[int, Path]) -> None:
    port, ca = https_origin
    p = start(port, ca, "")
    with p.client(verify=ca) as c:
        assert c.get(f"https://127.0.0.1:{port}/x").status_code == 200
    assert p.records("net.request") == []
    assert not (p.paths.data_dir / "egress-ca").exists()  # no CA made unless it's used


def test_plain_http_requests_follow_the_same_rules(
    start: Callable[..., Proxy], tmp_path: Path
) -> None:
    Handler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        p = start(port, None, RULES)
        # one client each: the plain-HTTP proxy serves one request per connection
        with httpx.Client(proxy=p.url(), timeout=10) as c:
            ok = c.get(f"http://127.0.0.1:{port}/repos/a")
        with httpx.Client(proxy=p.url(), timeout=10) as c:
            no = c.get(f"http://127.0.0.1:{port}/admin")
        assert ok.status_code == 200 and no.status_code == 403
        assert [s[1] for s in Handler.seen] == ["/repos/a"]
        assert {r.decision for r in p.records("net.request", 2)} == {"allow", "deny"}
    finally:
        srv.shutdown()


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl isn't installed")
@pytest.mark.skipif(os.name == "nt", reason="Windows' curl uses schannel, which ignores CA bundles")
def test_curl_through_inspection(
    start: Callable[..., Proxy], https_origin: tuple[int, Path]
) -> None:
    """A real tool that isn't Python: curl, trusting the bundle a run would give it."""
    port, ca = https_origin
    p = start(port, ca, RULES)
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env.update(cwtls.trust_env(p.bundle(), p.paths.data_dir / "egress-ca" / "ca-cert.pem"))
    base = ["curl", "-sS", "--proxy", p.url(), f"https://127.0.0.1:{port}"]
    out = subprocess.run([*base[:-1], base[-1] + "/repos/q"], env=env, capture_output=True,
                         text=True, timeout=30, check=False)  # fmt: skip
    assert out.returncode == 0 and out.stdout == "secret page /repos/q", out.stderr
    out = subprocess.run([*base[:-1], "-X", "DELETE", base[-1] + "/repos/q"], env=env,
                         capture_output=True, text=True, timeout=30, check=False)  # fmt: skip
    assert "denied" in out.stdout


# ------------------------------------------------------------------ rules and the CA


@pytest.mark.parametrize(
    ("path", "pattern", "matches"),
    [
        ("/repos/a/b", "/repos/**", True),
        ("/repos", "/repos/**", True),
        ("/repository", "/repos/**", False),
        ("/repos/a/b", "/repos/*", False),
        ("/repos/a", "/repos/*", True),
        ("/a.b", "/a?b", False),  # only * is special
        ("/", "/**", True),
    ],
)
def test_path_globs(path: str, pattern: str, matches: bool) -> None:
    assert path_matches(path, pattern) is matches


def test_request_path_drops_query_and_authority() -> None:
    assert request_path("/a/b?token=1") == "/a/b"
    assert request_path("https://h.example/a?x") == "/a"
    assert request_path("") == "/"


def test_first_matching_rule_wins_and_host_patterns() -> None:
    tls = TlsInspection(
        enabled=True,
        rules=[
            HttpRule(host="*.github.com", method="post", path="/**", decision="deny"),
            HttpRule(host="api.github.com:443", path="/**", decision="allow"),
        ],
        unmatched="deny",
    )
    assert not check_request(tls, "api.github.com", 443, "POST", "/x").allowed
    assert check_request(tls, "api.github.com", 443, "GET", "/x").allowed
    assert not check_request(tls, "api.github.com", 8443, "GET", "/x").allowed
    with pytest.raises(ValueError, match="start with"):
        HttpRule(host="x", path="repos", decision="allow")


def test_ca_is_private_stable_and_rotates(tmp_path: Path) -> None:
    folder = cwtls.ensure_ca(tmp_path)
    if os.name != "nt":
        assert stat.S_IMODE((folder / "ca-key.pem").stat().st_mode) == 0o600
    first = cwtls.ca_cert_pem(tmp_path)
    assert cwtls.ensure_ca(tmp_path) == folder and cwtls.ca_cert_pem(tmp_path) == first
    cert = x509.load_pem_x509_certificate(first)
    assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0
    cwtls.rotate_ca(tmp_path)
    assert cwtls.ca_fingerprint(cwtls.ca_cert_pem(tmp_path)) != cwtls.ca_fingerprint(first)


def test_privacy_warning_when_on(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "e.toml").write_text("[tls_inspection]\nenabled = true\n")
    (paths.config_dir / "environments" / "f.toml").write_text("")
    cfg = load_config(paths)
    assert any("tls_inspection is on" in w for w in cfg.privacy("e").warnings)
    assert not any("tls_inspection" in w for w in cfg.privacy("f").warnings)
