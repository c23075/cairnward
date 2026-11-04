"""Fuzzing of the parsers that take untrusted input (T3.1): the egress proxy (raw bytes
from sandboxed programs), MCP messages (from clients and from upstream servers), PDF
files, the redaction scanner and gateway request bodies.

Properties: never an unhandled exception or a 500; the service keeps working after a
bad input; nothing malformed is forwarded; regexes stay fast on hostile text.

Runs a modest number of examples by default; `CAIRNWARD_FUZZ=1000` runs more.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.test_egress import Running, origin, start  # noqa: F401 (fixtures)

EXAMPLES = int(os.environ.get("CAIRNWARD_FUZZ", "60"))
FUZZ = settings(max_examples=EXAMPLES, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture,
                                       HealthCheck.too_slow])  # fmt: skip


class Errors(logging.Handler):
    """Collects asyncio's 'unhandled exception' reports (a crashed connection handler)."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.seen: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.seen.append(record.getMessage() + repr(record.exc_info and record.exc_info[1]))


@pytest.fixture
def asyncio_errors() -> Any:
    handler = Errors()
    logger = logging.getLogger("asyncio")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


# ------------------------------------------------------------------ egress proxy

TOKENISH = st.text(st.characters(codec="latin-1", exclude_characters="\r\n"), max_size=80)
HOSTS = st.one_of(
    st.just("example.com"), st.just("a" * 64 + ".com"), st.just("a..b"), st.just(""),
    st.just("[::1"), st.just("[::1]"), st.just("[zz]"), st.just("ex\x00ample.com"),
    st.just("xn--zz"), st.just("example.com:99999"), st.just("example.com:-1"),
    st.text(st.characters(codec="latin-1", exclude_characters="\r\n /"), max_size=70),
)  # fmt: skip
METHODS = st.sampled_from(["GET", "CONNECT", "POST", "get", "connect", "", "G\x00T", "PRI"])


@st.composite
def proxy_requests(draw: Any, auth: str) -> bytes:
    method = draw(METHODS)
    host = draw(HOSTS)
    port = draw(st.sampled_from(["", ":80", ":443", ":0", ":x", ":65536", ":1"]))
    if method.upper() == "CONNECT":
        target = host + port
    else:
        scheme = draw(st.sampled_from(["http://", "https://", "", "http:/", "ftp://"]))
        target = scheme + host + port + draw(st.sampled_from(["/", "/p?q=1", "", "/\x00"]))
    version = draw(st.sampled_from(["HTTP/1.1", "HTTP/1.0", "", "HTTP/9"]))
    line = draw(st.sampled_from([f"{method} {target} {version}", f"{method} {target}",
                                 f"{method}  {target}  {version}", method]))  # fmt: skip
    headers = [f"Proxy-Authorization: {auth}"] if draw(st.booleans()) else []
    headers += draw(st.lists(st.one_of(
        st.builds(lambda k, v: f"{k}: {v}", TOKENISH, TOKENISH),
        st.just("X-Smuggle: a\nGET /admin HTTP/1.1"), st.just("no-colon-header"),
        st.just("Host: " + host), st.just(":"), st.just("Transfer-Encoding: chunked"),
    ), max_size=5))  # fmt: skip
    if draw(st.booleans()):
        draw_auth = headers[:1] if headers and headers[0].startswith("Proxy-") else []
        headers = draw_auth + headers[1:]
    return ("\r\n".join([line, *headers]) + "\r\n\r\n").encode("latin-1", "replace")


def _ask(running: Running, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", running.proxy.port), timeout=15) as s:
        s.sendall(data)
        with_shutdown = not data.endswith(b"\r\n\r\n")
        if with_shutdown:
            s.shutdown(socket.SHUT_WR)
        out = b""
        try:
            while chunk := s.recv(65536):
                out += chunk
        except (ConnectionResetError, TimeoutError):
            pass
        return out


def _resolver(host: str, port: int) -> list[str]:
    host.encode("idna")  # what getaddrinfo does first: UnicodeError on bad labels
    if "\x00" in host:
        raise socket.gaierror(-2, "Name or service not known")
    return ["127.0.0.1"]  # a special address: refused, so nothing leaves the machine


@FUZZ
@given(data=st.data())
def test_fuzz_egress_requests(start: Callable[..., Running], origin: int,  # noqa: F811
                              asyncio_errors: Errors, data: st.DataObject) -> None:  # fmt: skip
    running = getattr(test_fuzz_egress_requests, "_running", None)
    if running is None or running.proxy is None:
        running = start('network = "open"', resolver=_resolver)
        test_fuzz_egress_requests._running = running  # type: ignore[attr-defined]
    request = data.draw(st.one_of(proxy_requests(running.auth), st.binary(max_size=300)))
    reply = _ask(running, request)
    complete = b"\r\n\r\n" in request
    assert reply.startswith(b"HTTP/1.1 ") or (not complete and reply == b""), (request, reply[:200])
    status = int(reply[9:12]) if reply else 0
    assert status in (0, 400, 403, 407, 502), reply[:300]
    assert not asyncio_errors.seen, asyncio_errors.seen
    # still serving afterwards
    ok = running.get(f"http://127.0.0.1:{origin}/")
    assert ok.startswith(b"HTTP/1.1 403"), ok[:200]  # 127.0.0.1 isn't listed: refused


def test_egress_never_forwards_injected_headers(
    start: Callable[..., Running],  # noqa: F811
    origin: int,  # noqa: F811
) -> None:
    from tests.test_egress import Origin, allow_local

    running = start(allow_local(origin))
    smuggle = (f"GET http://127.0.0.1:{origin}/ HTTP/1.1\r\nProxy-Authorization: {running.auth}"
               f"\r\nX-A: 1\nGET /admin HTTP/1.1\r\n\r\n").encode()  # fmt: skip
    reply = running.raw(smuggle)
    assert reply.startswith(b"HTTP/1.1 400"), reply[:200]
    assert Origin.seen == []
    nul = (f"GET http://127.0.0.1:{origin}/ HTTP/1.1\r\nProxy-Authorization: {running.auth}"
           f"\r\nX-A: a\x00b\r\n\r\n").encode()  # fmt: skip
    assert running.raw(nul).startswith(b"HTTP/1.1 400")
    assert Origin.seen == []


@pytest.mark.parametrize(("request_line", "status", "why"), [
    ("CONNECT " + "a" * 64 + ".com:443 HTTP/1.1", 403, b"DNS lookup failed"),  # bad IDNA label
    ("CONNECT a..b:443 HTTP/1.1", 403, b"DNS lookup failed"),
    ("GET http://[::1/ HTTP/1.1", 400, b"malformed URL"),
    ("GET http://example.com:99999/ HTTP/1.1", 400, b"malformed URL"),
    ("GET http://example.com/ HTTP/1.1 extra", 400, b"malformed request line"),
])  # fmt: skip
def test_egress_bad_targets_get_clear_answers(start: Callable[..., Running],  # noqa: F811
                                              asyncio_errors: Errors, request_line: str,
                                              status: int, why: bytes) -> None:  # fmt: skip
    running = start('network = "open"')  # the real resolver: getaddrinfo
    reply = running.raw(f"{request_line}\r\nProxy-Authorization: {running.auth}\r\n\r\n"
                        .encode())  # fmt: skip
    assert reply.startswith(f"HTTP/1.1 {status}".encode()) and why in reply, reply[:300]
    assert not asyncio_errors.seen, asyncio_errors.seen


# ------------------------------------------------------------------ MCP from clients

SCALARS = (st.none() | st.booleans() | st.integers()
           | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=20))  # fmt: skip
JSON = st.recursive(
    SCALARS,
    lambda kids: (
        st.lists(kids, max_size=4) | st.dictionaries(st.text(max_size=8), kids, max_size=4)
    ),
    max_leaves=12,
)
METHODS_MCP = st.sampled_from(
    [
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/read",
        "nope",
        "",
    ]
)


@st.composite
def mcp_messages(draw: Any) -> Any:
    """Mostly plausible requests (so the handlers behind the parser are reached), some
    with odd shapes, and some arbitrary JSON."""
    msg: dict[str, Any] = {"jsonrpc": draw(st.sampled_from(["2.0", "2.0", "2.0", "1.0", 2]))}
    if draw(st.integers(0, 9)) < 9:
        msg["method"] = draw(METHODS_MCP) if draw(st.integers(0, 9)) < 8 else draw(JSON)
    if draw(st.integers(0, 9)) < 9:
        msg["id"] = draw(st.integers(0, 9)) if draw(st.integers(0, 9)) < 7 else draw(JSON)
    odd = st.sampled_from([[1], [], "x", "", 5, 0, True, None, {"name": 5}, [{"name": "x"}]])
    good = st.fixed_dictionaries(
        {"name": st.one_of(st.sampled_from(["cairnward__lit_search", "cairnward__lit_ask",
                                            "cairnward__artifact_write", "x__y", ""]), JSON),
         "arguments": st.one_of(odd, JSON, st.fixed_dictionaries(
             {"query": st.one_of(st.just("sleep"), JSON), "k": st.one_of(st.just(3), odd, JSON),
              "question": JSON, "library": st.one_of(st.just("thesis"), JSON), "path": JSON,
              "content": JSON})),
         "protocolVersion": JSON, "uri": JSON})  # fmt: skip
    if draw(st.integers(0, 9)) < 8:
        msg["params"] = draw(st.one_of(odd, good, good, JSON))
    return msg if draw(st.integers(0, 9)) < 9 else draw(JSON)


@pytest.fixture
def mcp_client(tmp_path: Path) -> Any:
    from tests.test_mcp_builtins import SKILL, setup

    p = __import__("cairnward.config.paths", fromlist=["Paths"]).Paths(
        tmp_path / "cfg", tmp_path / "data", tmp_path / "state")  # fmt: skip
    (p.config_dir / "environments").mkdir(parents=True)
    sk = p.config_dir / "skills" / "apa-review"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(SKILL)
    (p.config_dir / "environments" / "lit.toml").write_text(
        'skills = ["apa-review"]\ntools = ["research"]\n[privacy]\nisolation = "off"\n'
    )
    loop = asyncio.new_event_loop()
    client, hub, audit, _ = loop.run_until_complete(setup(p))
    yield loop, client
    loop.run_until_complete(client.aclose())
    loop.run_until_complete(hub.aclose())
    audit.close()
    loop.close()


@FUZZ
@given(msg=mcp_messages(), raw=st.one_of(*[st.none()] * 9, st.binary(max_size=60)))
def test_fuzz_mcp_from_clients(mcp_client: Any, msg: Any, raw: bytes | None) -> None:
    loop, client = mcp_client
    body = raw if raw is not None else json.dumps(msg).encode()
    resp = loop.run_until_complete(
        client.post("/mcp", content=body, headers={"content-type": "application/json"})
    )
    assert resp.status_code in (200, 202, 400), (resp.status_code, resp.text[:300])
    if resp.status_code == 200:
        reply = resp.json()
        assert reply.get("jsonrpc") == "2.0" and ("result" in reply or "error" in reply)


# ------------------------------------------------------------------ MCP from servers


@FUZZ
@given(lines=st.lists(st.one_of(
    st.builds(lambda m: json.dumps(m) + "\n", JSON),
    st.builds(lambda i, r: json.dumps({"jsonrpc": "2.0", "id": i, "result": r}) + "\n",
              st.one_of(st.just(1), JSON), JSON),
    st.builds(lambda i, e: json.dumps({"jsonrpc": "2.0", "id": i, "error": e}) + "\n",
              st.one_of(st.just(1), JSON), JSON),
    st.builds(lambda m, p: json.dumps({"jsonrpc": "2.0", "method": m, "params": p}) + "\n",
              st.text(max_size=20), JSON),
    st.builds(lambda m, i: json.dumps({"jsonrpc": "2.0", "method": m, "id": i}) + "\n",
              st.text(max_size=20), JSON),
    st.text(max_size=40).map(lambda t: t.replace("\n", " ") + "\n"),
), max_size=8))  # fmt: skip
def test_fuzz_mcp_from_servers(lines: list[str]) -> None:
    """Whatever an upstream server sends, a request ends with a result or McpError (the
    SDK parses it; Cairnward's adapter must not crash or hang)."""
    from cairnward.mcp.jsonrpc import McpError
    from cairnward.mcp.upstream import Connection

    async def go() -> None:
        reader = asyncio.StreamReader()
        sent: list[bytes] = []

        class Writer:
            def write(self, data: bytes) -> None:
                sent.append(data)

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        conn = Connection(reader, Writer())  # type: ignore[arg-type]
        for line in lines:
            reader.feed_data(line.encode())
        reader.feed_eof()
        with contextlib.suppress(McpError):
            await asyncio.wait_for(conn.list_tools(timeout=2), 5)
        await asyncio.wait_for(conn.close(), 10)

    asyncio.run(go())


# ------------------------------------------------------------------ redaction and the gateway

HOSTILE = st.one_of(
    st.text(max_size=2000),
    st.builds(lambda c, n: c * n, st.sampled_from(["a", "1", "@", ".", "-", " ", "a@", "1-",
                                                   "+1 ", "x.", "DE", "4"]), st.integers(1, 6000)),
)  # fmt: skip


@FUZZ
@given(text=HOSTILE)
def test_fuzz_redaction_is_fast(text: str) -> None:
    from cairnward.redaction import Scanner

    scanner = Scanner()
    started = time.monotonic()
    scanner.scan(text)
    assert time.monotonic() - started < 1.0, f"slow on {text[:40]!r}... ({len(text)} chars)"


@pytest.fixture
def gateway(tmp_path: Path) -> Any:
    from cairnward.audit import AuditLog
    from cairnward.backends import Backends
    from cairnward.config import load_config
    from cairnward.config.paths import Paths
    from cairnward.gateway import ClientKeys, GatewayDeps, create_app
    from cairnward.policy import PolicyEngine
    from cairnward.store import Vault
    from tests.fakes.backends import FakeOllama, FakeOpenAI
    from tests.test_gateway import BACKENDS

    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "backends.toml").write_text(BACKENDS)
    cfg = load_config(paths)
    fakes = {"local-ollama": FakeOllama(), "llama": FakeOpenAI(), "cloud": FakeOpenAI()}
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(
        fakes[n].handler))  # type: ignore[attr-defined]  # fmt: skip
    audit, vault = AuditLog.open(paths), Vault.open(paths)
    keys = ClientKeys.at(paths.state_dir)
    key = keys.add("fuzz")
    app = create_app(GatewayDeps(cfg, PolicyEngine(cfg, audit=audit), backends, keys, vault))
    loop = asyncio.new_event_loop()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://127.0.0.1:8484")  # fmt: skip
    yield loop, client, key
    loop.run_until_complete(client.aclose())
    vault.close()
    audit.close()
    loop.close()


@FUZZ
@given(body=st.one_of(JSON, st.fixed_dictionaries(
    {"model": st.one_of(st.sampled_from(["cw/coding:low", "cw/x", ""]), JSON),
     "messages": st.one_of(JSON, st.lists(st.fixed_dictionaries(
         {"role": st.one_of(st.sampled_from(["user", "system", "tool"]), JSON),
          "content": JSON}), max_size=3)),
     "input": JSON, "prompt": JSON, "stream": JSON, "max_tokens": JSON})))  # fmt: skip
def test_fuzz_gateway_bodies(gateway: Any, body: Any) -> None:
    loop, client, key = gateway
    for path in ("/v1/chat/completions", "/v1/embeddings", "/v1/completions"):
        resp = loop.run_until_complete(
            client.post(path, json=body, headers={"Authorization": f"Bearer {key}"})
        )
        assert resp.status_code < 500, (path, resp.status_code, resp.text[:300])


def test_redaction_scales_with_many_findings() -> None:
    """A pasted export with tens of thousands of addresses (once quadratic: 30 s)."""
    from cairnward.redaction import Scanner

    text = "contact a@b.co or +44 20 7946 0958 " * 20000
    started = time.monotonic()
    found = Scanner().scan(text)
    assert len(found) == 40000 and time.monotonic() - started < 5
