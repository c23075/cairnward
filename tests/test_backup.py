"""restic backups (FR-SYNC-1/3/6/8, D50): snapshots hold a per-conversation export;
restoring brings back what's missing; purging a conversation removes it from every
snapshot (the M3 test: purged content absent from all snapshots afterwards).
Skipped unless restic (>= 0.15) is installed."""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairnward import backup
from cairnward.cli import main
from cairnward.config import load_config
from cairnward.config.edit import change_backup
from cairnward.config.paths import Paths
from cairnward.store import Vault

pytestmark = pytest.mark.skipif(backup.restic() is None, reason="restic not installed")


@pytest.fixture
def cfg_paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    p.config_dir.mkdir(parents=True)
    (p.config_dir / "cairnward.toml").write_text(
        f'[backup]\nmode = "on_demand"\nrepository = "{(tmp_path / "repo").as_posix()}"\n'
        "keep_daily = 2\n"
    )
    return p


def seed(paths: Paths) -> dict[str, str]:
    with Vault.open(paths) as v:
        keep = v.create_conversation(environment="coding", client="aider", title="Keep me")
        v.add_message(keep, {"role": "user", "content": "the parser drops quotes"})
        gone = v.create_conversation(environment="coding", client="jan", title="Secret plan")
        v.add_message(gone, {"role": "user", "content": "SECRET-PLAN-TEXT"})
        locked = v.create_conversation(environment="coding", client="jan", title="Locked")
        v.add_message(locked, {"role": "user", "content": "behind a passphrase"})
        v.lock(locked, "correct horse battery staple")
    art = paths.data_dir / "artifacts" / "conversation" / gone
    art.mkdir(parents=True)
    (art / "plan.md").write_text("SECRET-PLAN-TEXT")
    return {"keep": keep, "gone": gone, "locked": locked}


def test_backup_restore_and_purge_from_every_snapshot(cfg_paths: Paths, tmp_path: Path) -> None:
    cfg = load_config(cfg_paths)
    ids = seed(cfg_paths)
    password = backup.init(cfg, log=lambda m: None)
    assert password and backup.password_file(cfg).stat().st_mode & 0o777 == 0o600
    first = backup.run(cfg, log=lambda m: None)
    assert first["conversations"] == 3 and first["snapshot"]
    assert not (backup.backup_dir(cfg) / "staging").exists()  # export not left behind
    with Vault.open(cfg_paths) as v:  # a second snapshot with more content
        v.add_message(ids["keep"], {"role": "assistant", "content": "fixed"})
    backup.run(cfg, log=lambda m: None)
    assert len(backup.snapshots(cfg)) == 2
    assert sorted(backup.remaining(cfg, list(ids.values()))) == sorted(ids.values())

    # the export: one folder per conversation; locked keys stay wrapped
    snap, export = backup.restore(cfg, tmp_path / "restored")
    conv = json.loads((export / "conversations" / ids["keep"] / "conversation.json").read_text())
    assert len(conv["messages"]) == 2 and conv["conversation"]["enc"] == "age"
    key = json.loads((export / "conversations" / ids["locked"] / "key.json").read_text())
    assert key["kind"] == "wrapped"

    # purge: gone from every snapshot (both of them), artifacts too
    with Vault.open(cfg_paths) as v:
        v.soft_delete(ids["gone"])
        v.purge(ids["gone"])
    assert [p["id"] for p in backup.pending(cfg)] == [ids["gone"]]
    assert backup.process_purges(cfg, log=lambda m: None) == 1
    assert backup.pending(cfg) == []
    assert backup.remaining(cfg, [ids["gone"]]) == []
    assert sorted(backup.remaining(cfg, [ids["keep"], ids["locked"]])) == sorted(
        [ids["keep"], ids["locked"]])  # fmt: skip
    assert len(backup.snapshots(cfg)) == 2  # rewritten, not dropped
    for snap in backup.snapshots(cfg):
        listing = backup.run_restic(cfg, "ls", "--json", snap["id"]).stdout
        assert ids["gone"] not in listing and ids["keep"] in listing

    # restore after losing the vault: everything that wasn't purged comes back
    for name in ("vault.db", "keys.db"):
        for f in cfg_paths.data_dir.glob(name + "*"):
            f.unlink()
    _snap, export = backup.restore(cfg, tmp_path / "again")
    assert backup.import_conversations(cfg, export) == {"added": 2, "skipped": 0}
    with Vault.open(cfg_paths) as v:
        texts = [m.body["content"] for m in v.messages(ids["keep"])]
        assert texts == ["the parser drops quotes", "fixed"]
        assert v.conversation(ids["locked"]).locked
        with pytest.raises(Exception):  # noqa: B017 - purged: not in any backup
            v.conversation(ids["gone"])
    # importing again adds nothing
    assert backup.import_conversations(cfg, export) == {"added": 0, "skipped": 2}


def test_purge_stays_pending_while_the_repository_is_away(cfg_paths: Paths) -> None:
    cfg = load_config(cfg_paths)
    ids = seed(cfg_paths)
    backup.init(cfg, log=lambda m: None)
    backup.run(cfg, log=lambda m: None)
    with Vault.open(cfg_paths) as v:
        v.soft_delete(ids["gone"])
        v.purge(ids["gone"])
    repo = Path(backup.repository(cfg))
    moved = repo.with_name("unplugged")
    repo.rename(moved)  # the backup disk is unplugged
    assert backup.process_purges(cfg, log=lambda m: None) == 0
    assert backup.pending(cfg)[0]["id"] == ids["gone"]
    moved.rename(repo)
    backup.run(cfg, log=lambda m: None)  # the next run catches up first
    assert backup.pending(cfg) == [] and backup.remaining(cfg, [ids["gone"]]) == []


def test_without_backups_purges_are_not_kept(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    with Vault.open(paths) as v:
        c = v.create_conversation(environment="coding", client="x", title="t")
        v.soft_delete(c)
        v.purge(c)
    assert len(backup.pending(cfg)) == 1
    assert backup.process_purges(cfg, log=lambda m: None) == 0 and backup.pending(cfg) == []


def test_settings_checks(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "cairnward.toml").write_text('[backup]\nrepository = "s3:s3.example/b"\n')
    assert backup.repository(load_config(paths)) == "s3:s3.example/b"  # remote, via egress
    assert backup.remote_kind(load_config(paths)) == "s3"
    (paths.config_dir / "cairnward.toml").write_text('[backup]\nrepository = "sftp:me@nas:/b"\n')
    with pytest.raises(backup.BackupError, match="wouldn't go through"):
        backup.repository(load_config(paths))
    assert backup.cloud_warning(Path("/home/me/Dropbox/backup"))
    assert backup.cloud_warning(Path("/media/usb/backup")) is None


def test_cli(cfg_paths: Paths, capsys: pytest.CaptureFixture[str],
             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    for k, v in (("CAIRNWARD_DATA_DIR", cfg_paths.data_dir),
                 ("CAIRNWARD_STATE_DIR", cfg_paths.state_dir)):  # fmt: skip
        monkeypatch.setenv(k, str(v))
    seed(cfg_paths)
    base = ["--config-dir", str(cfg_paths.config_dir), "backup"]
    assert main([*base, "init"]) == 0
    out = capsys.readouterr().out
    assert "password" in out.lower()
    assert main([*base, "run"]) == 0
    assert main([*base, "list"]) == 0
    assert "conversations" in capsys.readouterr().out
    assert main([*base, "status"]) == 0
    assert "snapshots: 1" in capsys.readouterr().out


def test_locked_items_can_still_be_opened_after_a_restore(cfg_paths: Paths, tmp_path: Path) -> None:
    """A locked job's or library's file is useless without its wrapped key, which isn't with the
    conversations' keys in an export. Real restic, a fresh machine's data folder at the end."""
    from dataclasses import replace

    from cairnward.store import crypto

    cfg = load_config(cfg_paths)
    secret = "correct horse battery staple"
    with Vault.open(cfg_paths) as v:
        v.create_conversation(environment="coding", client="aider", title="Any")
        key = v.lock_item("library:thesis", secret)
    code = tmp_path / "plugin-code"
    code.mkdir()
    (cfg_paths.config_dir / "mcp.toml").write_text(
        '[servers.research]\nkind = "plugin"\ncommand = ["x"]\nunsandboxed = true\n'
        "[servers.research.permissions]\nread = []\n"
    )
    cfg = load_config(cfg_paths)
    locks = cfg_paths.data_dir / "mcp" / "research" / "library-locks"
    locks.mkdir(parents=True)
    (locks / "thesis.age").write_bytes(crypto.seal(key, b"the-sealed-index"))
    backup.init(cfg, log=lambda m: None)
    backup.run(cfg, log=lambda m: None)
    _snap, export = backup.restore(cfg, tmp_path / "restored")
    assert "library:thesis" in (export / "locked-items" / "keys.json").read_text()
    sealed = (export / "plugins" / "research" / "library-locks" / "thesis.age").read_bytes()
    assert b"the-sealed-index" not in sealed

    fresh = replace(cfg_paths, data_dir=tmp_path / "new-data", state_dir=tmp_path / "new-state")
    new_cfg = load_config(fresh)
    for _ in range(2):  # importing twice changes nothing
        backup.import_conversations(new_cfg, export)
    with Vault.open(fresh) as v:
        assert crypto.open_sealed(v.open_item("library:thesis", secret), sealed) == (
            b"the-sealed-index"
        )


def modes(folder: Path) -> set[int]:
    return {stat.S_IMODE(p.stat().st_mode) & 0o077 for p in [folder, *folder.rglob("*")]}


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_who_can_read_the_folder_is_your_choice(cfg_paths: Paths, tmp_path: Path) -> None:
    """Real restic, whose files start read-only, including those added by later backups."""
    repo = tmp_path / "repo"
    cfg = load_config(cfg_paths)
    seed(cfg_paths)
    backup.init(cfg, log=lambda m: None)
    backup.run(cfg, log=lambda m: None)
    assert modes(repo) == {0}  # only you, by default

    cfg, changed = change_backup(cfg, "on_demand", str(repo), access="group")
    assert changed == ["access"]
    backup.run(cfg, log=lambda m: None)
    assert modes(repo) <= {0o050, 0o040} and 0o050 in modes(repo)  # group reads, never writes
    assert all(not (stat.S_IMODE(p.stat().st_mode) & 0o007) for p in repo.rglob("*"))
    assert backup.snapshots(cfg)  # restic still works with it
    assert b"SECRET-PLAN-TEXT" not in b"".join(
        p.read_bytes() for p in repo.rglob("*") if p.is_file()
    )

    cfg, _ = change_backup(cfg, "on_demand", str(repo), access="owner")
    backup.apply_access(cfg)
    assert modes(repo) == {0}


def test_default_folder_and_turning_it_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """`backup use` with no folder: the default one, set up and working."""
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir()
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    assert (
        main(["--config-dir", str(paths.config_dir), "backup", "use", "--mode", "on_demand"]) == 0
    )
    out = capsys.readouterr().out
    default = tmp_path / "cairnward-backups"
    assert str(default) in out and "only you" in out and "same disk" in out
    assert "password:" in out and (default / "config").exists()
    assert "repository" not in paths.main_config.read_text().split("[backup]")[1].split("mode")[0]
    cfg = load_config(paths)
    assert cfg.main.backup.mode == "on_demand" and backup.initialised(cfg)
    assert main(["--config-dir", str(paths.config_dir), "backup", "run"]) == 0


def test_choosing_a_folder_and_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir()
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    chosen = tmp_path / "my backups"
    assert main(["--config-dir", str(paths.config_dir), "backup", "use", str(chosen),
                 "--access", "group"]) == 0  # fmt: skip
    assert "you and your group" in capsys.readouterr().out
    cfg = load_config(paths)
    assert (cfg.main.backup.repository, cfg.main.backup.access) == (str(chosen), "group")
    assert backup.repository(cfg) == chosen and backup.initialised(cfg)


def test_same_disk_warning(cfg_paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(cfg_paths)
    cfg_paths.data_dir.mkdir(parents=True, exist_ok=True)
    assert "same disk" in str(backup.same_disk_warning(cfg))
    real_stat = Path.stat

    def other_disk(self: Path, **kw: object) -> object:
        result = real_stat(self)
        if str(self).startswith(str(cfg_paths.data_dir)):
            return SimpleNamespace(st_dev=result.st_dev + 1)
        return result

    monkeypatch.setattr(Path, "stat", other_disk)
    assert backup.same_disk_warning(cfg) is None
