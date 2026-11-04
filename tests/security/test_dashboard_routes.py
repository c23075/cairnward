"""Dashboard routes beyond sign-in: what locked, missing or unavailable things answer, that
sensitive actions are audited, and that downloads can't render in the page."""

import shutil
from pathlib import Path
from typing import Any

import pytest

from cairnward.artifacts import ArtifactStore
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.dashboard.app import DashboardAuth, dashboard_app
from tests.security.test_local_api import PASS, env, signed_in  # noqa: F401  (fixture)


def actions(audit: Any) -> list[str]:
    return [r.action for r in audit.records()]


async def test_a_locked_conversation_answers_423_and_opens_with_the_passphrase(env: Any) -> None:  # noqa: F811
    app, auth, audit, vault, *_ = env
    conv = vault.create_conversation(environment="coding", title="Private")
    vault.add_message(conv, {"role": "user", "content": "my diary"})
    c, csrf = await signed_in(app, auth)
    hdr = {"X-CSRF-Token": csrf}
    try:
        assert (await c.post(f"/api/vault/{conv}/lock", json={"passphrase": PASS},
                             headers=hdr)).status_code == 200  # fmt: skip
        shown = await c.get(f"/api/vault/{conv}")
        assert shown.status_code == 423 and "my diary" not in shown.text
        assert (await c.get(f"/api/vault/{conv}/export")).status_code == 423
        wrong = await c.post(f"/api/vault/{conv}/unlock", json={"passphrase": "not the passphrase"},
                             headers=hdr)  # fmt: skip
        assert wrong.status_code == 423
        assert (await c.post(f"/api/vault/{conv}/unlock", json={"passphrase": PASS, "minutes": 1},
                             headers=hdr)).status_code == 200  # fmt: skip
        assert "my diary" in (await c.get(f"/api/vault/{conv}")).text
        assert (await c.post(f"/api/vault/{conv}/relock", headers=hdr)).status_code == 200
        assert (await c.get(f"/api/vault/{conv}")).status_code == 423
    finally:
        await c.aclose()
    done = actions(audit)
    assert "vault.lock" in done and "vault.unlock" in done
    assert not any(PASS in r.to_json_line() for r in audit.records())


async def test_vault_operations_and_downloads(env: Any) -> None:  # noqa: F811
    app, auth, audit, vault, *_ = env
    conv = vault.create_conversation(environment="coding", title="Notes")
    vault.add_message(conv, {"role": "user", "content": "hello"})
    c, csrf = await signed_in(app, auth)
    hdr = {"X-CSRF-Token": csrf}
    try:
        for fmt, needle in (("md", "hello"), ("json", '"content"')):
            out = await c.get(f"/api/vault/{conv}/export", params={"format": fmt})
            assert needle in out.text
            assert out.headers["content-disposition"].startswith("attachment;")
        assert "vault.export" in actions(audit)

        async def post(op: str, **body: Any) -> Any:
            return await c.post(f"/api/vault/{conv}/{op}", json=body, headers=hdr)

        assert (await post("title", title="Renamed")).status_code == 200
        assert (await post("pin", pinned=False)).status_code == 200
        assert (await post("unknown-op")).status_code == 404
        assert (await post("purge")).status_code == 400  # not in the trash yet
        assert vault.conversation(conv).title == "Renamed"
        assert (await post("delete")).status_code == 200
        assert (await post("restore")).status_code == 200
        assert (await post("purge", force=True)).status_code == 200
        assert "vault.purge" in actions(audit)
        missing = await c.post("/api/vault/no-such-id/title", json={"title": "x"}, headers=hdr)
        assert missing.status_code == 404
    finally:
        await c.aclose()


async def test_audit_and_egress_filters_and_exports(env: Any) -> None:  # noqa: F811
    app, auth, audit, *_ = env
    audit.append("model.call", actor="client:jan", client="jan", environment="coding",
                 outcome="ok", details={"prompt_tokens": 1})  # fmt: skip
    c, _ = await signed_in(app, auth)
    try:
        assert (await c.get("/api/audit", params={"since": "soon"})).status_code == 400
        assert (await c.get("/api/egress", params={"since": "soon"})).status_code == 400
        assert [r["client"] for r in (await c.get("/api/audit", params={"client": "jan"})).json()
                ] == ["jan"]  # fmt: skip
        assert (await c.get("/api/audit", params={"client": "other"})).json() == []
        assert (await c.get("/api/activity", params={"after": 10**6})).json() == []
        for path, params in (("/api/audit/export", {}), ("/api/egress", {"format": "csv"})):
            out = await c.get(path, params=params)
            assert out.status_code == 200
            assert out.headers["content-disposition"].startswith("attachment;")
    finally:
        await c.aclose()


async def test_answering_approvals(env: Any) -> None:  # noqa: F811
    app, auth, *_, inbox, _paths = env
    c, csrf = await signed_in(app, auth)
    hdr = {"X-CSRF-Token": csrf}
    try:
        gone = await c.post("/api/approvals/nope", json={"granted": True, "scope": "once"},
                            headers=hdr)  # fmt: skip
        assert gone.status_code == 404
        gone = await c.post("/api/approvals/nope", json={"granted": True, "scope": "once",
                                                         "similar": True}, headers=hdr)  # fmt: skip
        assert gone.status_code == 404
        waited = await c.get("/api/approvals/wait", params={"version": inbox.version, "timeout": 0})
        assert waited.json() == {"version": inbox.version, "pending": []}
    finally:
        await c.aclose()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_artifact_routes(env: Any) -> None:  # noqa: F811
    app, auth, audit, *_, paths = env
    store = ArtifactStore(paths.data_dir)
    first = store.write("job", "abc123", "page.svg", "<svg/>")
    store.write("job", "abc123", "page.svg", "<svg>2</svg>")
    store.write("job", "abc123", "notes.txt", "plain")
    c, csrf = await signed_in(app, auth)
    try:
        listed = (await c.get("/api/artifacts/job/abc123")).json()
        assert listed["files"] == ["notes.txt", "page.svg"] and len(listed["log"]) == 3
        file = await c.get(
            "/api/artifacts/job/abc123/file", params={"path": "page.svg", "rev": first}
        )
        assert file.text == "<svg/>" and file.headers["content-type"].startswith("text/plain")
        history = await c.get("/api/artifacts/job/abc123/history", params={"path": "page.svg"})
        assert len(history.json()) == 2
        restored = await c.post("/api/artifacts/job/abc123/restore", headers={"X-CSRF-Token": csrf},
                                json={"path": "page.svg", "rev": first})  # fmt: skip
        assert restored.status_code == 200 and "artifact.restore" in actions(audit)
        assert store.read("job", "abc123", "page.svg") == b"<svg/>"
        for name, media in (("page.svg", "image/svg+xml"), ("notes.txt", "text/plain")):
            shown = await c.get("/api/preview/job/abc123", params={"path": name})
            assert shown.headers["content-type"].startswith(media)
            assert shown.headers["content-security-policy"].startswith("sandbox;")
        missing = await c.get("/api/artifacts/job/abc123/file", params={"path": "none.md"})
        assert missing.status_code == 404
        assert (await c.get("/api/artifacts/nope/abc123")).status_code == 404
        traversal = {"path": "../../etc/passwd"}
        escape = await c.get("/api/artifacts/job/abc123/file", params=traversal)
        assert escape.status_code == 404
    finally:
        await c.aclose()


@pytest.mark.parametrize("path", ["/api/approvals", "/api/activity", "/api/audit/export",
                                  "/api/vault/x/export", "/api/jobs"])  # fmt: skip
async def test_a_part_that_isnt_running_says_so(tmp_path: Path, path: str) -> None:
    cfg = load_config(Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state"))
    auth = DashboardAuth()
    c, _ = await signed_in(dashboard_app(cfg, auth=auth, audit=None), auth)
    try:
        assert (await c.get(path)).status_code == 503
    finally:
        await c.aclose()


async def test_mcp_routes_for_unknown_servers(env: Any) -> None:  # noqa: F811
    app, auth, *_ = env
    c, csrf = await signed_in(app, auth)
    try:
        assert (await c.get("/api/mcp/servers")).json() == []
        assert (await c.get("/api/mcp/nope/tools")).status_code == 404
        body = {"tools": {"x": "hash"}, "environment": "coding"}
        refused = await c.post("/api/mcp/nope/approve", json=body, headers={"X-CSRF-Token": csrf})
        assert refused.status_code == 404
    finally:
        await c.aclose()
