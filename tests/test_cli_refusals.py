"""Where a command refuses or changes nothing: approvals answered in the terminal, vault
passphrases, agent runs, and backups that would go around egress."""

import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward import approvals_cli, backup, vault_cli
from cairnward.cli import main
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.run.runner import RunError, run_agent
from cairnward.store import Vault

SECRET = "correct horse battery staple 42"


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    p.config_dir.mkdir(parents=True)
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(p.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(p.state_dir))
    return p


# ------------------------------------------------------------------ approvals in the terminal


def request(**extra: Any) -> dict[str, Any]:
    return {"id": "r1", "risk": "high", "kind": "net.connect", "target": "example.org",
            "reason": "new host", "initiated_by": "model", "age_s": 3, **extra}  # fmt: skip


class Server:
    """The approvals endpoints, over a real httpx client."""

    def __init__(self, *rounds: list[dict[str, Any]]):
        self.rounds, self.answers = list(rounds), list[dict[str, Any]]()

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            self.answers.append(json.loads(req.content))
            return httpx.Response(200, json={})
        pending = self.rounds.pop(0) if self.rounds else []
        if req.url.path.endswith("/wait"):
            return httpx.Response(200, json={"version": 1, "pending": pending})
        return httpx.Response(200, json={"pending": pending})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url="http://x")


def test_list_pending_text_and_json() -> None:
    out = io.StringIO()
    approvals_cli.list_pending(Server([]).client(), out, as_json=False)
    assert "nothing waiting" in out.getvalue()
    out = io.StringIO()
    approvals_cli.list_pending(Server([request()]).client(), out, as_json=False)
    assert "example.org" in out.getvalue()
    out = io.StringIO()
    approvals_cli.list_pending(Server([request()]).client(), out, as_json=True)
    assert json.loads(out.getvalue())[0]["id"] == "r1"


def test_watch_keeps_asking_until_the_answer_is_an_option() -> None:
    server, out = Server([request()]), io.StringIO()
    code = approvals_cli.watch(
        server.client(), io.StringIO("maybe\nn\n"), out, notify=False, once=True
    )
    assert code == 0 and "not one of the options" in out.getvalue()
    assert server.answers == [{"granted": False, "scope": "once", "similar": False}]


def test_watch_end_of_input_answers_nothing() -> None:
    server = Server([request()])
    assert approvals_cli.watch(server.client(), io.StringIO(""), io.StringIO(), notify=False) == 0
    assert server.answers == []


@pytest.mark.parametrize("typed", [["short", "short"], [SECRET, "something else"]])
def test_a_bad_passphrase_turns_yes_into_no(
    monkeypatch: pytest.MonkeyPatch, typed: list[str]
) -> None:
    replies = iter(typed)
    monkeypatch.setattr(approvals_cli, "ask_passphrase", lambda _prompt: next(replies))
    server, out = Server([request(needs_secret=True)]), io.StringIO()
    approvals_cli.watch(server.client(), io.StringIO("y\n"), out, notify=False, once=True)
    assert "answered no" in out.getvalue()
    assert server.answers == [{"granted": False, "scope": "once", "similar": False}]


def test_a_good_passphrase_goes_with_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals_cli, "ask_passphrase", lambda _prompt: SECRET)
    server, out = Server([request(needs_secret=True)]), io.StringIO()
    approvals_cli.watch(server.client(), io.StringIO("y\n"), out, notify=False, once=True)
    assert server.answers[0]["granted"] and server.answers[0]["secret"] == SECRET
    assert SECRET not in out.getvalue()


def test_notification_holds_no_content_beyond_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/notify-send")

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", run)
    approvals_cli._notify(request())
    assert calls[0][-1] == "net.connect example.org"


# ------------------------------------------------------------------ vault passphrases


def cli(paths: Paths, *args: str) -> int:
    return main(["--config-dir", str(paths.config_dir), "vault", *args])


def conversation(paths: Paths) -> str:
    with Vault.open(paths) as v:
        cid = v.create_conversation(environment="coding", title="Notes")
        v.add_message(cid, {"role": "user", "content": "hello"})
        return cid


def typing(monkeypatch: pytest.MonkeyPatch, *typed: str) -> None:
    replies = iter(typed)
    monkeypatch.setattr(vault_cli, "ask_passphrase", lambda _prompt: next(replies))


def test_lock_with_differing_passphrases_changes_nothing(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cid = conversation(paths)
    typing(monkeypatch, SECRET, "something else")
    assert cli(paths, "lock", cid) == 1
    assert "not locked" in capsys.readouterr().out
    with Vault.open(paths) as v:
        assert not v.is_locked(cid)


def test_recover_with_differing_passphrases_changes_nothing(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = conversation(paths)
    with Vault.open(paths) as v:
        recovery = v.create_recovery_key()
        v.lock(cid, SECRET)
    typing(monkeypatch, recovery, "a new passphrase", "a different one")
    assert cli(paths, "recover", cid) == 1
    with Vault.open(paths) as v:
        v.unlock(cid, SECRET, minutes=1)  # the old passphrase still works
        assert v.readable(cid)


def test_unlock_needs_the_running_server_but_lock_removal_doesnt(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cid = conversation(paths)
    with Vault.open(paths) as v:
        v.lock(cid, SECRET)
    typing(monkeypatch, SECRET)
    assert cli(paths, "unlock", cid, "--minutes", "1") == 1
    assert "isn't running" in capsys.readouterr().out
    typing(monkeypatch, SECRET)
    assert cli(paths, "unlock", cid, "--permanently") == 0
    with Vault.open(paths) as v:
        assert not v.is_locked(cid)


def test_search_prune_and_empty_lists(paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(paths, "list") == 0
    assert "no conversations" in capsys.readouterr().out
    assert cli(paths, "list", "--trash") == 0
    assert "trash is empty" in capsys.readouterr().out
    assert cli(paths, "search", "nothing here") == 0
    assert "no matches" in capsys.readouterr().out
    assert cli(paths, "prune") == 0
    assert "purged 0" in capsys.readouterr().out


# ------------------------------------------------------------------ agent runs


@pytest.mark.parametrize(("kw", "message"), [
    ({"command": []}, "nothing to run"),
    ({"command": ["true"], "environment": "nope"}, "unknown environment"),
    ({"command": ["true"], "profile_name": "nope"}, "unknown profile"),
    ({"command": ["/no/such/program"]}, "command not found"),
])  # fmt: skip
def test_run_refuses_before_starting_anything(
    paths: Paths, kw: dict[str, Any], message: str
) -> None:
    with pytest.raises(RunError, match=message):
        run_agent(load_config(paths), **kw)


# ------------------------------------------------------------------ backups


def with_backup(paths: Paths, repository: str) -> Any:
    (paths.config_dir / "cairnward.toml").write_text(
        f"schema_version = 1\n[backup]\nmode = 'on_demand'\nrepository = '{repository}'\n"
    )
    return load_config(paths)


def test_no_repository_means_the_default_folder(paths: Paths) -> None:
    assert backup.repository(load_config(paths)) == Path.home() / "cairnward-backups"


@pytest.mark.parametrize("repository", ["sftp:me@example.org:/backups", "ftp:example.org/x"])
def test_a_remote_that_would_bypass_egress_is_refused(paths: Paths, repository: str) -> None:
    with pytest.raises(backup.BackupError):
        backup.repository(with_backup(paths, repository))


def test_a_remote_backup_needs_the_running_server(paths: Paths) -> None:
    cfg = with_backup(paths, "rest:https://backup.example.org/repo")
    assert backup.repository(cfg) == "rest:https://backup.example.org/repo"
    with pytest.raises(backup.BackupError), backup.remote_access(cfg):
        raise AssertionError("must not get here without egress")


def test_folder_backup_needs_no_server(paths: Paths, tmp_path: Path) -> None:
    cfg = with_backup(paths, str(tmp_path / "repo"))
    with backup.remote_access(cfg):
        assert backup.repository(cfg) == tmp_path / "repo"


SNAPS = [{"id": "aaaa1111", "short_id": "aaaa", "time": "2026-01-01T10:00:00"},
         {"id": "bbbb2222", "short_id": "bbbb", "time": "2026-02-01T10:00:00"}]  # fmt: skip


def test_picking_a_snapshot(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(paths)
    monkeypatch.setattr(backup, "snapshots", lambda _cfg: [])
    with pytest.raises(backup.BackupError, match="no backups yet"):
        backup.pick(cfg)
    monkeypatch.setattr(backup, "snapshots", lambda _cfg: SNAPS)
    assert backup.pick(cfg)["id"] == "bbbb2222"
    assert backup.pick(cfg, snapshot="aaaa")["id"] == "aaaa1111"
    assert backup.pick(cfg, at="2026-01-15")["id"] == "aaaa1111"
    with pytest.raises(backup.BackupError, match="no snapshot"):
        backup.pick(cfg, snapshot="cccc")
    with pytest.raises(backup.BackupError, match="or earlier"):
        backup.pick(cfg, at="2025-01-01")


def test_restoring_something_that_isnt_a_cairnward_backup(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(paths)
    monkeypatch.setattr(backup, "snapshots", lambda _cfg: SNAPS)
    monkeypatch.setattr(backup, "run_restic", lambda *_a, **_k: "")
    with pytest.raises(backup.BackupError, match=r"no MANIFEST\.json"):
        backup.restore(cfg, tmp_path / "out")


# ------------------------------------------------------------------ the coverage rule itself


def report(**packages: tuple[int, int]) -> dict[str, Any]:
    return {"files": {f"src/cairnward/{name.replace('__', '/')}.py": {"summary": {
        "covered_lines": covered, "num_statements": total, "covered_branches": 0,
        "num_branches": 0}} for name, (covered, total) in packages.items()}}  # fmt: skip


def test_coverage_rule_goes_by_package_and_counts_new_code(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "check_coverage", Path(__file__).parent.parent / "scripts" / "check_coverage.py"
    )
    assert spec and spec.loader
    rule = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rule)

    def result(**packages: tuple[int, int]) -> int:
        out = tmp_path / "coverage.json"
        out.write_text(json.dumps(report(**packages)))
        return int(rule.main(["check", str(out)]))

    assert result(policy__a=(80, 100), audit__b=(100, 100)) == 0
    # a strong package doesn't hide a weak one, although the average here is 90%
    assert result(policy__a=(990, 1000), audit__b=(70, 100)) == 1
    assert result(brand_new=(1, 100)) == 1  # counted unless someone adds it to the list
    assert result(cli=(1, 100), core__server=(1, 100), policy__a=(90, 100)) == 0  # named ones
    assert result(core__management=(1, 100)) == 1  # only core/server.py is left out of core
    assert result() == 1  # an empty report isn't a pass
