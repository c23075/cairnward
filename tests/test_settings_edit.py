"""Changing privacy settings from the dashboard: only enumerated settings, the whole
configuration must still load, weakening needs confirmation, a built-in environment is
written out before its first change, comments survive, every change is audited; and
the backups view's data (no repository paths of remotes)."""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from cairnward import backup
from cairnward.audit import AuditLog, enabled_for, record_settings
from cairnward.config import load_config
from cairnward.config.edit import EditError, change_backup, change_privacy, options
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


def test_change_backup_rules(paths: Paths) -> None:
    cfg = load_config(paths)
    for mode, repo, why in (("weekly", "/mnt/b", "mode must be"), ("auto", None, "needs a folder"),
                            ("auto", "relative/path", "full path"),
                            ("auto", "s3:s3.example.org/bucket", "full path")):  # fmt: skip
        with pytest.raises(EditError, match=why):
            change_backup(cfg, mode, repo)
    assert "[backup]" not in paths.main_config.read_text()  # refusals write nothing
    new, changed = change_backup(cfg, "on_demand", "/mnt/b", 12)
    assert (new.main.backup.mode, new.main.backup.repository) == ("on_demand", "/mnt/b")
    assert new.main.backup.interval_hours == 12 and set(changed) == {"mode", "repository",
                                                                     "interval_hours"}  # fmt: skip
    text = paths.main_config.read_text()
    assert "# my notes" in text and "chosen on purpose" in text  # comments survive
    with pytest.raises(EditError, match="wouldn't load"):
        change_backup(new, "on_demand", "/mnt/b", 0)  # below the allowed interval
    assert load_config(paths).main.backup.interval_hours == 12  # the failed change was undone
    _, changed = change_backup(new, "on_demand", "/mnt/b", 12)
    assert changed == []
    off, _ = change_backup(new, "none", None)
    assert off.main.backup.mode == "none" and off.main.backup.repository is None


def test_change_backup_leaves_a_remote_alone(paths: Paths) -> None:
    remote = "s3:s3.example.org/private-bucket/cairnward"
    with paths.main_config.open("a") as f:
        f.write(f'[backup]\nmode = "on_demand"\nrepository = "{remote}"\n')
    new, _ = change_backup(load_config(paths), "auto", None)
    assert new.main.backup.mode == "auto" and new.main.backup.repository == remote


async def test_backup_now_with_real_restic(paths: Paths, tmp_path: Path) -> None:
    """The button: refused while off, then sets up a real repository and takes a snapshot;
    the settings route is audited without the path, and CSRF applies to both."""
    if backup.restic() is None:
        pytest.skip("restic not installed")
    cfg = load_config(paths)
    auth = DashboardAuth()
    audit = AuditLog.open(paths, enabled_for(cfg))
    vault = Vault.open(paths)
    c, csrf = await signed_in(dashboard_app(cfg, auth=auth, audit=audit, vault=vault), auth)
    headers = {"X-CSRF-Token": csrf}
    folder = str(tmp_path / "private-backup-folder")
    try:
        assert (await c.post("/api/backup/run")).status_code == 403  # no CSRF token
        assert (await c.post("/api/backup/run", headers=headers)).status_code == 400  # off
        body = {"mode": "on_demand", "repository": folder}
        assert (await c.post("/api/backup/settings", json=body)).status_code == 403
        r = await c.post("/api/backup/settings", json=body, headers=headers)
        assert r.json() == {"changed": ["mode", "repository"], "restart_needed": True}
        assert (await c.get("/api/backup")).json()["settings_saved"] is True
        bad = await c.post("/api/backup/settings", json={"mode": "on_demand", "repository": "x"},
                           headers=headers)  # fmt: skip
        assert bad.status_code == 400 and "full path" in bad.json()["detail"]
    finally:
        await c.aclose()
    # after the restart a saved change needs
    cfg = load_config(paths)
    auth = DashboardAuth()
    calls: list[int] = []

    def counted() -> dict[str, Any]:
        calls.append(1)
        return backup.run_now(cfg)

    c, csrf = await signed_in(dashboard_app(cfg, auth=auth, audit=audit, vault=vault,
                                            backup_now=counted), auth)  # fmt: skip
    headers = {"X-CSRF-Token": csrf}
    try:
        assert (await c.post("/api/backup/run", headers=headers)).status_code == 202
        for _ in range(600):
            state = (await c.get("/api/backup")).json()
            if not state["running"]:
                break
            await asyncio.sleep(0.1)
        assert state["run_error"] is None and state["last"]["snapshot"] and calls == [1]
        assert len(backup.snapshots(cfg)) == 1
    finally:
        await c.aclose()
        vault.close()
    records = audit.records()
    assert {"backup.requested", "backup.settings", "backup.run"} <= {r.action for r in records}
    assert not any("private-backup-folder" in r.to_json_line() for r in records
                   if r.action == "backup.settings")  # fmt: skip
    audit.close()


async def test_a_failed_backup_is_shown_and_a_second_is_refused(paths: Paths) -> None:
    with paths.main_config.open("a") as f:
        f.write('[backup]\nmode = "on_demand"\nrepository = "/nonexistent/x"\n')
    cfg = load_config(paths)
    auth = DashboardAuth()
    release = threading.Event()

    def fails() -> dict[str, Any]:
        release.wait(10)
        raise backup.BackupError("the disk isn't mounted")

    c, csrf = await signed_in(dashboard_app(cfg, auth=auth, audit=None, backup_now=fails), auth)
    headers = {"X-CSRF-Token": csrf}
    try:
        assert (await c.post("/api/backup/run", headers=headers)).status_code == 202
        assert (await c.post("/api/backup/run", headers=headers)).status_code == 409
        release.set()
        for _ in range(100):
            state = (await c.get("/api/backup")).json()
            if not state["running"]:
                break
            await asyncio.sleep(0.05)
        assert state["run_error"] == "the disk isn't mounted"
    finally:
        await c.aclose()


def test_control_route_runs_the_backup_for_the_isolated_core() -> None:
    from fastapi.testclient import TestClient

    from cairnward.egress.control import control_app
    from cairnward.egress.proxy import Sessions

    def refuse() -> dict[str, Any]:
        raise backup.BackupError("backups are off")

    ok = TestClient(control_app(Sessions(), backup_now=lambda: {"snapshot": "abc"}))
    assert ok.post("/v1/backup/run").json() == {"snapshot": "abc"}
    r = TestClient(control_app(Sessions(), backup_now=refuse)).post("/v1/backup/run")
    assert r.status_code == 400 and r.json()["detail"] == "backups are off"
    assert TestClient(control_app(Sessions())).post("/v1/backup/run").status_code == 404
