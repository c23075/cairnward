"""What the owner does from the dashboard, against a real isolated `serve` and real restic: the
core is in the sandbox, so these only work if the sandbox lets them. Skipped unless srt is
installed."""

import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.artifacts import ArtifactStore
from cairnward.backup import restic
from cairnward.store import Vault
from tests.isolation.harness import Cairnward

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
SECRET = "correct horse battery staple"


def signed_in(c: Cairnward) -> httpx.Client:
    out = c.cw("dashboard", "--no-open", check=True).stdout
    url = next(line for line in out.splitlines() if line.startswith("http"))
    client = httpx.Client(base_url=f"http://127.0.0.1:{c.dash}", timeout=60)
    client.get(url.replace("http://localhost", "http://127.0.0.1"))
    csrf = client.get("/api/session").json()["csrf"]
    client.headers["x-csrf-token"] = csrf
    return client


def seed(c: Cairnward) -> str:
    with Vault.open(c.paths) as vault:
        conv = vault.create_conversation(environment="coding", client="aider", title="Secret plan")
        ArtifactStore(c.paths.data_dir, keys=vault).write("conversation", conv, "plan.md", "step 1")
    return conv


def test_settings_change_from_the_dashboard(tmp_path: Path) -> None:
    c = Cairnward(tmp_path, "isolated", privacy='preset = "balanced"\n')
    with c:
        ui = signed_in(c)
        change = {"scope": "global", "setting": "network", "value": "allowlist"}
        got = ui.post("/api/settings/privacy", json=change)
        assert got.status_code == 200, got.text
        weaker = {"scope": "global", "setting": "network", "value": "open"}
        assert ui.post("/api/settings/privacy", json=weaker).status_code == 409  # needs confirm
        backup = {"mode": "on_demand", "repository": str(tmp_path / "b"), "interval_hours": 24,
                  "access": "owner"}  # fmt: skip
        saved = ui.post("/api/backup/settings", json=backup)
        assert saved.status_code == 200 and saved.json()["changed"], saved.text
        kinds = {r["action"] for r in c.audit()}
    assert "allowlist" in (c.paths.config_dir / "cairnward.toml").read_text()
    assert "settings.changed" in kinds and "backup.settings" in kinds


@pytest.mark.skipif(restic() is None, reason="restic not installed")
def test_back_up_now_from_the_dashboard(tmp_path: Path) -> None:
    c = Cairnward(tmp_path, "isolated", privacy='preset = "balanced"\n')
    seed(c)
    repo = tmp_path / "backups"
    c.cw("backup", "use", str(repo), "--mode", "on_demand", check=True)
    with c:
        ui = signed_in(c)
        started = ui.post("/api/backup/run")
        assert started.status_code == 202, started.text
        state: dict[str, Any] = {}
        for _ in range(120):
            state = ui.get("/api/backup").json()
            if not state.get("running"):
                break
            time.sleep(1)
        assert not state.get("running") and not state.get("error"), state
    assert c.cw("backup", "list", check=True).stdout.strip()
    restored = tmp_path / "restored"
    c.cw("backup", "restore", "--to", str(restored), check=True)
    assert list(restored.rglob("conversation.json"))


def test_locking_a_conversation_and_its_artifacts_from_the_dashboard(tmp_path: Path) -> None:
    c = Cairnward(tmp_path, "isolated", privacy='preset = "balanced"\n')
    conv = seed(c)
    plan = {"path": "plan.md"}
    with c:
        ui = signed_in(c)
        files = f"/api/artifacts/conversation/{conv}/file"
        assert ui.get(files, params=plan).status_code == 200
        lock = ui.post(f"/api/vault/{conv}/lock", json={"passphrase": SECRET})
        assert lock.status_code == 200, lock.text
        assert ui.get(files, params=plan).status_code != 200
        wrong = ui.post(f"/api/vault/{conv}/unlock", json={"passphrase": "wrong one"})
        assert wrong.status_code >= 400
        opened = ui.post(f"/api/vault/{conv}/unlock", json={"passphrase": SECRET, "minutes": 5})
        assert opened.status_code == 200, opened.text
        assert "step 1" in ui.get(files, params=plan).text
        assert ui.post(f"/api/vault/{conv}/relock").status_code == 200
        assert ui.get(files, params=plan).status_code != 200
    sealed = list((c.paths.data_dir / "artifacts" / "conversation" / conv).rglob("*"))
    assert sealed and not any(p.is_file() and b"step 1" in p.read_bytes() for p in sealed)
