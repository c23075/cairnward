"""Dashboard hardening (spec §3.1, ADR-0005, FR-DASH-1): Host check, single-use login
links, HttpOnly SameSite=Strict session, CSRF token + Origin on changes, strict CSP,
no CORS, no path tricks; and that its actions work."""

import shutil
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.dashboard import DashboardAuth, dashboard_app
from cairnward.dashboard import app as dash_module
from cairnward.policy import ApprovalInbox, ApprovalRequest
from cairnward.store import Vault

BASE = "http://127.0.0.1:8485"
PASS = "dashboard passphrase 1"


@pytest.fixture
def env(tmp_path: Path) -> Any:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    audit, vault, inbox, auth = (
        AuditLog.open(paths),
        Vault.open(paths),
        ApprovalInbox(10),
        DashboardAuth(),
    )
    app = dashboard_app(cfg, auth=auth, audit=audit, vault=vault, inbox=inbox)
    yield app, auth, audit, vault, inbox, paths
    audit.close()
    vault.close()


async def signed_in(app: Any, auth: DashboardAuth) -> tuple[httpx.AsyncClient, str]:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
    resp = await c.get(f"/login?t={auth.new_link_token()}")
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    csrf = (await c.get("/api/session")).json()["csrf"]
    return c, csrf


async def test_host_login_and_session(env: Any) -> None:
    app, auth, audit, *_ = env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as anon:
        assert (await anon.get("/", headers={"Host": "evil.example:8485"})).status_code == 421
        assert (await anon.get("/api/session")).status_code == 401
        assert (await anon.get("/login?t=forged")).status_code == 403
        token = auth.new_link_token()
        assert (await anon.get(f"/login?t={token}")).status_code == 303
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as other:
        assert (await other.get(f"/login?t={token}")).status_code == 403  # single use
    # expired links
    stale = auth.new_link_token()
    auth._links[stale] = time.monotonic() - 1
    assert auth.redeem(stale) is None
    assert [r.action for r in audit.records()] == ["dashboard.login"]


async def test_csrf_origin_and_headers(env: Any) -> None:
    app, auth, *_ = env
    c, csrf = await signed_in(app, auth)
    try:
        page = await c.get("/")
        csp = page.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert page.headers["x-frame-options"] == "DENY"
        api = await c.get("/api/session")
        assert api.headers["cache-control"] == "no-store"
        assert "access-control-allow-origin" not in api.headers
        preflight = {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
        pre = await c.options("/api/logout", headers=preflight)
        assert "access-control-allow-origin" not in pre.headers
        # changes need the CSRF token and (if sent) our own Origin
        assert (await c.post("/api/vault/folders", json={"name": "x"})).status_code == 403
        evil = {"X-CSRF-Token": csrf, "Origin": "https://evil.example"}
        bad = await c.post("/api/vault/folders", json={"name": "x"}, headers=evil)
        assert bad.status_code == 403
        ok = await c.post("/api/vault/folders", json={"name": "x"},
                          headers={"X-CSRF-Token": csrf, "Origin": BASE})  # fmt: skip
        assert ok.status_code == 200
        for path in ("/static/../app.py", "/static/%2e%2e%2fapp.py", "/static/nope.js"):
            assert (await c.get(path)).status_code == 404
        assert (await c.get("/static/app.js")).headers["content-type"].startswith("text/javascript")
        await c.post("/api/logout", headers={"X-CSRF-Token": csrf})
        assert (await c.get("/api/session")).status_code == 401
    finally:
        await c.aclose()


async def test_actions(env: Any) -> None:
    app, auth, _, vault, inbox, paths = env
    conv = vault.create_conversation(environment="coding", title="Notes")
    vault.add_message(conv, {"role": "user", "content": "hello"})
    c, csrf = await signed_in(app, auth)
    hdr = {"X-CSRF-Token": csrf}
    try:
        listed = (await c.get("/api/vault")).json()
        assert [x["title"] for x in listed] == ["Notes"]
        assert (await c.get("/api/vault", params={"q": "hello"})).json()[0]["snippet"] == "hello"
        assert (
            await c.post(f"/api/vault/{conv}/lock", json={"passphrase": PASS}, headers=hdr)
        ).status_code == 200
        assert (await c.get(f"/api/vault/{conv}")).status_code == 423
        assert (
            await c.post(f"/api/vault/{conv}/unlock", json={"passphrase": "wrong"}, headers=hdr)
        ).status_code == 423
        assert (
            await c.post(f"/api/vault/{conv}/unlock", json={"passphrase": PASS}, headers=hdr)
        ).status_code == 200
        assert (await c.get(f"/api/vault/{conv}")).json()["messages"][0]["body"][
            "content"
        ] == "hello"
        assert (await c.post(f"/api/vault/{conv}/delete", headers=hdr)).status_code == 200
        assert (await c.get("/api/vault", params={"trash": "true"})).json()[0]["id"] == conv
        assert (await c.post(f"/api/vault/{conv}/restore", headers=hdr)).status_code == 200
        # approvals answered from the dashboard
        out: list[Any] = []
        req = ApprovalRequest(kind="net.connect", target="x.org:443", risk="medium", reason="ask",
                              environment="coding", conversation_id=None, client="c",
                              initiated_by="model")  # fmt: skip
        t = threading.Thread(target=lambda: out.append(inbox.request(req)))
        t.start()
        for _ in range(200):
            pending = (await c.get("/api/approvals")).json()["pending"]
            if pending:
                break
            time.sleep(0.01)
        r = await c.post(f"/api/approvals/{pending[0]['id']}", json={"granted": True}, headers=hdr)
        assert r.json() == {"answered": 1}
        t.join(5)
        assert out[0].granted and out[0].by == "user (dashboard)"
        assert (await c.get("/api/audit", params={"action": "vault.lock"})).json()[0][
            "target"
        ] == conv
        assert (await c.get("/api/audit/verify")).json()["ok"] is True
        assert (await c.get("/api/egress")).json() == []
        assert "global" in (await c.get("/api/settings")).json()
    finally:
        await c.aclose()
    for f in [*paths.data_dir.rglob("*"), *paths.state_dir.rglob("*")]:
        if f.is_file():
            assert PASS.encode() not in f.read_bytes(), f


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_artifact_preview_is_sandboxed(env: Any) -> None:
    from cairnward.artifacts import ArtifactStore

    app, auth, *_, paths = env
    ArtifactStore(paths.data_dir).write("job", "abc123", "page.html", "<script>x()</script>")
    c, _csrf = await signed_in(app, auth)
    try:
        page = await c.get("/")
        assert "frame-src 'self'" in page.headers["content-security-policy"]
        pv = await c.get("/api/preview/job/abc123", params={"path": "page.html"})
        csp = pv.headers["content-security-policy"]
        assert csp.startswith("sandbox;") and "allow-scripts" not in csp
        assert "default-src 'none'" in csp and "script-src" not in csp
        assert pv.headers["x-frame-options"] == "SAMEORIGIN"
        assert pv.headers["content-type"].startswith("text/html")
        assert (await c.get("/api/preview/nope/abc123", params={"path": "x"})).status_code == 404
        listed = (await c.get("/api/artifacts")).json()
        assert listed == [{"scope": "job", "owner": "abc123", "files": 1}]
    finally:
        await c.aclose()


def test_static_files_never_use_innerhtml() -> None:
    """XSS guard: the page builds DOM from data with textContent only."""
    for f in dash_module.STATIC.glob("*.js"):
        text = f.read_text()
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert banned not in text, f"{f.name} uses {banned}"
