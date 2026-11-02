"""Changing privacy settings from the dashboard: only enumerated settings, the whole
configuration must still load, weakening needs confirmation, a built-in environment is
written out before its first change, comments survive, every change is audited; and
the backups view's data (no repository paths of remotes)."""

from pathlib import Path
from typing import Any

import pytest

from cairnward.audit import AuditLog, record_settings
from cairnward.config import load_config
from cairnward.config.edit import EditError, change_privacy, options
from cairnward.config.paths import Paths
from cairnward.dashboard import DashboardAuth, dashboard_app
from cairnward.store import Vault
from tests.security.test_local_api import signed_in


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    p.config_dir.mkdir(parents=True)
    (p.config_dir / "cairnward.toml").write_text(
        '# my notes\n[privacy]\npreset = "strict"  # chosen on purpose\n[server]\n'
        "gateway_port = 8480\n"
    )
    return p


def test_options() -> None:
    o = options()
    assert o["network"] == ["open", "ask", "allowlist", "off"]
    assert o["preset"] == ["open", "balanced", "strict"] and "network_allow_hosts" not in o


def test_change_rules(paths: Paths) -> None:
    cfg = load_config(paths)
    with pytest.raises(EditError, match="can't be changed here"):
        change_privacy(cfg, "global", "network_allow_hosts", "x")
    with pytest.raises(EditError, match="must be one of"):
        change_privacy(cfg, "global", "network", "sometimes")
    with pytest.raises(EditError, match="unknown environment"):
        change_privacy(cfg, "nope", "network", "off")
    before = paths.main_config.read_text()
    with pytest.raises(EditError, match=r"needs confirmation.*redaction = warn"):
        change_privacy(cfg, "global", "redaction", "warn")
    assert paths.main_config.read_text() == before  # nothing left behind
    new, changes = change_privacy(cfg, "global", "redaction", "warn", confirm=True)
    text = paths.main_config.read_text()
    assert 'redaction = "warn"' in text and "# my notes" in text and "on purpose" in text
    assert new.privacy().settings["redaction"].value == "warn"
    assert {c.scope for c in changes} >= {"global", "coding"}
    # back to Strict's value needs no confirmation; removing goes back to the preset
    change_privacy(new, "global", "redaction", None)
    assert "redaction" not in paths.main_config.read_text()
    # a built-in environment is written out first, keeping its other settings
    cfg = load_config(paths)
    builtin_tools = cfg.environments["litreview-online"].tools
    new, _ = change_privacy(cfg, "litreview-online", "network", "off")
    env_file = paths.environments_dir / "litreview-online.toml"
    assert env_file.exists() and new.environments["litreview-online"].tools == builtin_tools
    assert new.environments["litreview-online"].research_sources
    assert new.privacy("litreview-online").settings["network"].value == "off"
    # a change that would make the configuration invalid isn't written
    (paths.environments_dir / "bad.toml").write_text('[privacy]\nnetwork = "off"\n')
    cfg = load_config(paths)
    (paths.environments_dir / "bad.toml").write_text('[privacy]\nnetwork = "off"\nfoo = 1\n')
    with pytest.raises(EditError, match="wouldn't load"):
        change_privacy(cfg, "global", "network", "off")
    assert 'network = "off"' not in paths.main_config.read_text()


async def test_dashboard_endpoint(paths: Paths) -> None:
    cfg = load_config(paths)
    with AuditLog.open(paths) as audit, Vault.open(paths) as vault:
        record_settings(cfg, audit)
        auth = DashboardAuth()
        app = dashboard_app(cfg, auth=auth, audit=audit, vault=vault)
        c, csrf = await signed_in(app, auth)
        hdr = {"X-CSRF-Token": csrf}
        try:
            body: dict[str, Any] = {"scope": "coding", "setting": "network", "value": "open"}
            r = await c.post("/api/settings/privacy", json=body, headers=hdr)
            assert r.status_code == 409 and "network = open (coding)" in r.json()["detail"]
            r = await c.post("/api/settings/privacy", json={**body, "confirm": True}, headers=hdr)
            assert r.status_code == 200 and r.json()["restart_needed"]
            s = (await c.get("/api/settings")).json()
            assert s["saved_since_start"][0]["after"] == "open"
            assert s["recovery_key"]["set_up"] is False
            # without the CSRF token: refused
            r = await c.post("/api/settings/privacy", json=body)
            assert r.status_code == 403
            b = (await c.get("/api/backup")).json()
            assert b["mode"] == "none" and b["pending"] == []
        finally:
            await c.aclose()
        changed = [r for r in audit.records() if r.action == "settings.changed"]
        assert changed and changed[-1].actor == "user (dashboard)"
        assert any(x["after"] == "open" for x in changed[-1].details["changes"])


async def test_backup_view_hides_remote_paths(paths: Paths) -> None:
    with paths.main_config.open("a") as f:
        f.write('[backup]\nmode = "on_demand"\n'
                'repository = "s3:s3.example.org/private-bucket/cairnward"\n')  # fmt: skip
    cfg = load_config(paths)
    auth = DashboardAuth()
    app = dashboard_app(cfg, auth=auth, audit=None)
    c, _ = await signed_in(app, auth)
    try:
        b = (await c.get("/api/backup")).json()
    finally:
        await c.aclose()
    assert b["where"] == "s3:s3.example.org"
    assert b["remote"] == "s3" and b["environment"] == "coding"
