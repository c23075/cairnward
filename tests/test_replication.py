"""Replication through a shared folder (FR-SYNC-4, D72): devices are separate state folders on
this machine and the shared folder is a real directory, with real encryption, signatures,
SQLite and git. Nothing here goes between two machines or through Syncthing itself; the
out-of-order test copies files one at a time, the way a folder sync delivers them."""

from __future__ import annotations

import random
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from cairnward import replication
from cairnward.artifacts import SEALED, ArtifactError, ArtifactStore
from cairnward.cli import main
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.store import LockedError, Vault
from cairnward.store.schema import conversations, messages

PASS = "correct horse battery staple"


class Device:
    def __init__(self, base: Path, name: str):
        self.paths = Paths(base / name / "c", base / name / "d", base / name / "s")
        self.paths.config_dir.mkdir(parents=True)
        self.name = name

    @property
    def cfg(self) -> Any:
        return load_config(self.paths)

    def vault(self) -> Vault:
        return Vault.open(self.paths)

    def sync(self) -> dict[str, Any]:
        return replication.sync(self.cfg)

    @property
    def id(self) -> str:
        me = replication.load_identity(self.cfg)
        assert me is not None
        return me.device

    def approve(self, other: Device) -> None:
        replication.set_device(self.cfg, other.id, "approved")


def pair(base: Path, folder: Path, *names: str) -> list[Device]:
    """The first device sets up the folder; the others join and are approved."""
    first = Device(base, names[0])
    key = replication.init(first.cfg, folder, name=names[0], mode="on_demand")
    devices = [first]
    for name in names[1:]:
        d = Device(base, name)
        replication.join(d.cfg, folder, key, name=name, mode="on_demand")
        for old in devices:
            old.approve(d)
        devices.append(d)
    return devices


def settle(*devices: Device) -> None:
    for _ in range(4):
        for d in devices:
            assert d.sync()["problems"] == []


def snapshot(d: Device) -> dict[str, Any]:
    with d.vault() as v, v._db.connect() as conn:
        convs = {r.id: (r.title, r.environment, r.client, r.folder_id, bool(r.pinned),
                        bool(r.archived), r.deleted_at, r.purged_at, r.updated_at, r.enc)
                 for r in conn.execute(select(conversations))}  # fmt: skip
        msgs = {r.id: (r.conversation_id, r.parent_id, r.seq, r.role, r.lamport, r.origin,
                       bytes(r.body)) for r in conn.execute(select(messages))}  # fmt: skip
    return {"conversations": convs, "messages": msgs}


def folder_bytes(folder: Path) -> bytes:
    return b"".join(f.read_bytes() for f in sorted(folder.rglob("*")) if f.is_file())


@pytest.fixture
def two(tmp_path: Path) -> tuple[Device, Device, Path]:
    folder = tmp_path / "shared"
    a, b = pair(tmp_path, folder, "a", "b")
    return a, b, folder


def test_a_conversation_reaches_the_other_device(two: tuple[Device, Device, Path]) -> None:
    a, b, _folder = two
    with a.vault() as v:
        conv = v.create_conversation(environment="coding", client="aider", title="The parser")
        m1 = v.add_message(conv, {"role": "user", "content": "it drops quotes"})
        v.add_message(conv, {"role": "assistant", "content": "fixed"}, parent_id=m1, model="m")
    settle(a, b)
    with b.vault() as v:
        assert v.conversation(conv).title == "The parser"
        got = v.messages(conv)
    assert [m.body["content"] for m in got] == ["it drops quotes", "fixed"]
    assert got[1].parent_id == m1 and got[1].model == "m"
    # ... and a reply from there comes back
    with b.vault() as v:
        v.add_message(conv, {"role": "user", "content": "thanks"}, parent_id=got[1].id)
    settle(a, b)
    with a.vault() as v:
        assert v.messages(conv)[-1].body["content"] == "thanks"
    assert snapshot(a) == snapshot(b)


def test_folder_holds_no_plaintext_even_without_conversation_encryption(
    two: tuple[Device, Device, Path],
) -> None:
    a, b, folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="TITLE-TEXT", environment="ENV-TEXT")
        v.add_message(conv, {"role": "user", "content": "BODY-TEXT-1"})
        plain = v.create_conversation(title="TITLE-PLAIN", crypto_shred=False)
        v.add_message(plain, {"role": "user", "content": "BODY-TEXT-2"})
        v.create_folder("FOLDER-TEXT")
    settle(a, b)
    blob = folder_bytes(folder)
    for text in (b"TITLE-TEXT", b"ENV-TEXT", b"BODY-TEXT", b"TITLE-PLAIN", b"FOLDER-TEXT",
                 b"AGE-SECRET-KEY"):  # fmt: skip
        assert text not in blob
    with b.vault() as v:
        assert v.messages(plain)[0].body["content"] == "BODY-TEXT-2"


def test_concurrent_turns_fork_and_both_are_kept(two: tuple[Device, Device, Path]) -> None:
    a, b, _folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Fork")
        root = v.add_message(conv, {"role": "user", "content": "root"})
    settle(a, b)
    with a.vault() as v:
        a1 = v.add_message(conv, {"role": "assistant", "content": "from a"}, parent_id=root)
        a2 = v.add_message(conv, {"role": "user", "content": "a again"}, parent_id=a1)
    with b.vault() as v:
        b1 = v.add_message(conv, {"role": "assistant", "content": "from b"}, parent_id=root)
    settle(a, b)
    assert snapshot(a) == snapshot(b)
    with a.vault() as v:
        got = v.messages(conv)
    assert {m.id for m in got} == {root, a1, a2, b1}
    parents = {m.id: m.parent_id for m in got}
    assert parents[a1] == parents[b1] == root
    # a fork's messages stay together
    order = [m.id for m in got]
    assert order.index(a2) == order.index(a1) + 1
    # and it's visible when shown
    from cairnward.vault_cli import to_markdown

    with a.vault() as v:
        assert "a fork" in to_markdown(v.conversation(conv), got)


def test_fields_merge_one_by_one(two: tuple[Device, Device, Path]) -> None:
    a, b, _folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Fields")
    settle(a, b)
    with a.vault() as v:
        v.set_pinned(conv, True)
    with b.vault() as v:
        v.set_title(conv, "Renamed on b")
    settle(a, b)
    assert snapshot(a) == snapshot(b)
    with a.vault() as v:
        got = v.conversation(conv)
    assert got.pinned and got.title == "Renamed on b"


def test_trash_and_folders_follow(two: tuple[Device, Device, Path]) -> None:
    a, b, _folder = two
    with a.vault() as v:
        f = v.create_folder("Work")
        conv = v.create_conversation(title="Tidy")
        v.move_to_folder(conv, f)
    settle(a, b)
    with b.vault() as v:
        assert v.conversation(conv).folder_id == f
        v.soft_delete(conv)
    settle(a, b)
    with a.vault() as v:
        assert v.conversation(conv).deleted_at is not None
        v.restore(conv)
    settle(a, b)
    with b.vault() as v:
        assert v.conversation(conv).deleted_at is None


def test_purge_reaches_every_device_and_leaves_the_folder(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    a, b, c = pair(tmp_path, folder, "a", "b", "c")
    with a.vault() as v:
        conv = v.create_conversation(title="Gone soon")
        v.add_message(conv, {"role": "user", "content": "text"})
    art = ArtifactStore(a.paths.data_dir)
    art.write("conversation", conv, "plan.md", "the plan")
    settle(a, b, c)
    assert ArtifactStore(b.paths.data_dir).head(conv)
    # c has published to it too, then goes away and keeps writing to it
    with c.vault() as v:
        v.add_message(conv, {"role": "user", "content": "from c"})
    settle(a, b, c)
    with c.vault() as v:
        v.add_message(conv, {"role": "user", "content": "written while away"})
    with b.vault() as v:
        v.purge(conv, force=True)
    b.sync()
    a.sync()
    assert not list(folder.glob(f"devices/{a.id}/*/{conv}*"))
    assert not list(folder.glob(f"devices/{b.id}/*/{conv}*"))
    assert list(folder.glob(f"devices/{c.id}/*/{conv}*"))  # not until it has synced
    settle(a, b, c)
    assert not list(folder.rglob(f"{conv}*"))
    for d in (a, b, c):
        with d.vault() as v:
            assert v.conversation(conv).purged_at is not None
            assert v._key_row(conv) is None
        assert ArtifactStore(d.paths.data_dir).head(conv) is None
    assert snapshot(a) == snapshot(b) == snapshot(c)


def test_a_new_device_does_not_bring_a_purged_conversation_back(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    a, b = pair(tmp_path, folder, "a", "b")
    with a.vault() as v:
        conv = v.create_conversation(title="Gone")
    settle(a, b)
    with a.vault() as v:
        v.purge(conv, force=True)
    settle(a, b)
    key = replication.format_key(replication.load_identity(a.cfg).sync_key)  # type: ignore[union-attr]
    c = Device(tmp_path, "c")
    replication.join(c.cfg, folder, key, mode="on_demand")
    settle(c)
    with c.vault() as v:
        assert v.conversation(conv).purged_at is not None


def test_a_lock_on_one_device_locks_it_everywhere(two: tuple[Device, Device, Path]) -> None:
    a, b, folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Private")
        v.add_message(conv, {"role": "user", "content": "SECRET-BODY"})
    ArtifactStore(a.paths.data_dir).write("conversation", conv, "n.md", "SECRET-ARTIFACT")
    settle(a, b)
    assert (b.paths.data_dir / "artifacts/conversation" / conv / ".git").exists()
    plain_key_on_b_side = (folder / "devices" / a.id / "k" / conv).read_bytes()
    with b.vault() as v:
        v.lock(conv, PASS)
    settle(a, b)
    assert (folder / "devices" / a.id / "k" / conv).exists() is False  # the old copy has gone
    assert plain_key_on_b_side not in folder_bytes(folder)
    with a.vault() as v:
        assert v.is_locked(conv)
        with pytest.raises(LockedError):
            v.messages(conv)
        v.unlock(conv, PASS, minutes=1)
        assert v.messages(conv)[0].body["content"] == "SECRET-BODY"
    # this device's artifacts are sealed too, and with the same key
    repo = a.paths.data_dir / "artifacts/conversation" / conv
    assert (repo / SEALED).exists() and not (repo / ".git").exists()
    # the lock can be removed from either side
    with a.vault() as v:
        v.unlock(conv, PASS, permanently=True)
    settle(a, b)
    with b.vault() as v:
        assert not v.is_locked(conv)
        assert v.messages(conv)[0].body["content"] == "SECRET-BODY"
    assert ArtifactStore(b.paths.data_dir).read("conversation", conv, "n.md") == b"SECRET-ARTIFACT"


def test_a_new_device_waits_for_approval(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    a = Device(tmp_path, "a")
    key = replication.init(a.cfg, folder, mode="on_demand")
    b = Device(tmp_path, "b")
    replication.join(b.cfg, folder, key, mode="on_demand")
    with b.vault() as v:
        conv = v.create_conversation(title="From the new device")
    b.sync()
    report = a.sync()
    assert report["pending_devices"] == [b.id]
    with a.vault() as v:
        assert v.list_conversations() == []
    a.approve(b)
    a.sync()
    with a.vault() as v:
        assert v.conversation(conv).title == "From the new device"


def test_a_blocked_device_can_not_write_as_itself_or_as_another(
    two: tuple[Device, Device, Path],
) -> None:
    a, b, folder = two
    replication.set_device(a.cfg, b.id, "blocked")
    with b.vault() as v:
        conv = v.create_conversation(title="Blocked")
    b.sync()
    a.sync()
    with a.vault() as v:
        assert v.list_conversations() == []
    # b puts its log in an approved device's name: its signature doesn't match that device
    other = Device(tmp_path_of(folder), "z")
    key = replication.format_key(replication.load_identity(a.cfg).sync_key)  # type: ignore[union-attr]
    replication.join(other.cfg, folder, key, mode="on_demand")
    a.approve(other)
    forged = folder / "devices" / other.id / "c" / f"{conv}.log"
    shutil.copy(folder / "devices" / b.id / "c" / f"{conv}.log", forged)
    report = a.sync()
    assert any("signature" in p for p in report["problems"])
    with a.vault() as v:
        assert v.list_conversations() == []


def tmp_path_of(folder: Path) -> Path:
    return folder.parent


@pytest.mark.parametrize("damage", ["flip", "swap", "extra-device-key"])
def test_changed_files_are_refused(two: tuple[Device, Device, Path], damage: str) -> None:
    a, b, folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Tamper")
        first = v.add_message(conv, {"role": "user", "content": "one"})
        v.add_message(conv, {"role": "user", "content": "two"}, parent_id=first)
    a.sync()
    log = folder / "devices" / a.id / "c" / f"{conv}.log"
    data = bytearray(log.read_bytes())
    if damage == "flip":
        data[len(data) // 2] ^= 1
        log.write_bytes(data)
    elif damage == "swap":  # the same records in another order
        records = []
        pos = 0
        while pos < len(data):
            n = int.from_bytes(data[pos : pos + 4], "big")
            records.append(bytes(data[pos : pos + 4 + n]))
            pos += 4 + n
        log.write_bytes(b"".join(reversed(records)))
    else:  # a key file moved to another conversation's name
        moved = folder / "devices" / a.id / "k" / "00000000-0000-0000-0000-000000000000"
        shutil.copy(folder / "devices" / a.id / "k" / conv, moved)
    report = b.sync()
    assert report["problems"], damage
    if damage != "extra-device-key":
        with b.vault() as v:
            if any(c.id == conv for c in v.list_conversations()):
                assert v.messages(conv) == []


def test_shortened_log_is_reported_not_taken_as_deletion(two: tuple[Device, Device, Path]) -> None:
    a, b, folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Short")
        v.add_message(conv, {"role": "user", "content": "one"})
    settle(a, b)
    log = folder / "devices" / a.id / "c" / f"{conv}.log"
    log.write_bytes(log.read_bytes()[:10])
    assert any("shorter" in p for p in b.sync()["problems"])
    with b.vault() as v:
        assert len(v.messages(conv)) == 1


def test_half_written_record_is_finished_after_a_crash(two: tuple[Device, Device, Path]) -> None:
    a, b, folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Crash")
        v.add_message(conv, {"role": "user", "content": "one"})
    settle(a, b)
    log = folder / "devices" / a.id / "c" / f"{conv}.log"
    with log.open("ab") as fh:  # a record cut short
        fh.write(b"\x00\x00\x10\x00partial")
    assert b.sync()["problems"] == []  # just not there yet
    with a.vault() as v:
        v.add_message(conv, {"role": "user", "content": "two"})
    settle(a, b)
    with b.vault() as v:
        assert [m.body["content"] for m in v.messages(conv)] == ["one", "two"]
    assert snapshot(a) == snapshot(b)


def test_crash_after_writing_before_noting_it_publishes_nothing_twice(
    two: tuple[Device, Device, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, _folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Crash")
        v.add_message(conv, {"role": "user", "content": "one"})

    def crash(*_a: Any, **_k: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(replication._Sync, "read_log", crash)
    with pytest.raises(KeyboardInterrupt):
        a.sync()
    monkeypatch.undo()
    before = snapshot(a)["messages"]
    assert all(row[4] is None for row in before.values())
    settle(a, b)
    after = snapshot(a)["messages"]
    assert all(row[4] is not None for row in after.values())
    assert snapshot(a) == snapshot(b)


def test_an_unmounted_folder_is_refused_and_nothing_is_written(
    two: tuple[Device, Device, Path],
) -> None:
    a, _b, folder = two
    moved = folder.with_name("unplugged")
    folder.rename(moved)
    folder.mkdir()
    with pytest.raises(replication.ReplicationError, match="not mounted"):
        a.sync()
    assert list(folder.iterdir()) == []
    assert replication.status(a.cfg)["problem"]


def test_a_link_or_an_oversized_file_in_the_folder_is_refused(
    two: tuple[Device, Device, Path], tmp_path: Path
) -> None:
    a, b, folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Fine")
    a.sync()
    big = folder / "devices" / a.id / "k" / "00000000-0000-0000-0000-000000000000"
    with big.open("wb") as fh:
        fh.truncate(replication.MAX_RECORD + 100)
    assert any("too large" in p for p in b.sync()["problems"])
    with b.vault() as v:
        assert v.conversation(conv).title == "Fine"
    # someone replaces this device's folder with a link to somewhere else
    mine = folder / "devices" / b.id / "c"
    shutil.rmtree(mine)
    mine.symlink_to(tmp_path)
    with pytest.raises(replication.ReplicationError, match="replaced by a link"):
        b.sync()


def test_doctor_and_dashboard_report_it(two: tuple[Device, Device, Path]) -> None:
    from cairnward.doctor import check_replication

    a, _b, folder = two
    assert check_replication(a.cfg).detail == "no sync yet"
    a.sync()
    assert check_replication(a.cfg).status == "ok"
    extra = Device(folder.parent, "x")
    key = replication.format_key(replication.load_identity(a.cfg).sync_key)  # type: ignore[union-attr]
    replication.join(extra.cfg, folder, key, mode="on_demand")
    assert "waiting for approval" in check_replication(a.cfg).detail
    folder.rename(folder.with_name("unplugged"))
    assert check_replication(a.cfg).status == "fail"
    seen = replication.status(a.cfg, check_folder=False)
    assert seen["problem"] is None  # the isolated core can't reach it: it doesn't claim to know
    assert "sync_key" not in str(seen) and "seed" not in str(seen)


def test_join_needs_the_right_key(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    a = Device(tmp_path, "a")
    replication.init(a.cfg, folder, mode="on_demand")
    b = Device(tmp_path, "b")
    wrong = replication.format_key(b"\x01" * 32)
    with pytest.raises(replication.ReplicationError, match="doesn't open"):
        replication.join(b.cfg, folder, wrong)
    with pytest.raises(replication.ReplicationError, match="isn't a sync key"):
        replication.join(b.cfg, folder, "nonsense")
    with pytest.raises(replication.ReplicationError, match="no cairnward-sync"):
        replication.join(b.cfg, tmp_path, wrong)
    assert replication.load_identity(b.cfg) is None


def test_excluded_environments_stay_on_this_device(two: tuple[Device, Device, Path]) -> None:
    a, b, folder = two
    text = a.paths.main_config.read_text()
    a.paths.main_config.write_text(
        text.replace("[replication]", '[replication]\nexclude_environments = ["private"]')
    )
    with a.vault() as v:
        kept = v.create_conversation(environment="private", title="Stays")
        shared = v.create_conversation(environment="coding", title="Goes")
    ArtifactStore(a.paths.data_dir).write("conversation", kept, "n.md", "x")
    settle(a, b)
    assert not list(folder.rglob(f"{kept}*"))
    with b.vault() as v:
        assert [c.id for c in v.list_conversations()] == [shared]


def test_artifacts_merge_the_same_on_both_devices(two: tuple[Device, Device, Path]) -> None:
    a, b, _folder = two
    with a.vault() as v:
        conv = v.create_conversation(title="Art")
    sa, sb = ArtifactStore(a.paths.data_dir), ArtifactStore(b.paths.data_dir)
    sa.write("conversation", conv, "both.md", "start")
    settle(a, b)
    assert sb.read("conversation", conv, "both.md") == b"start"
    sa.write("conversation", conv, "a.md", "only a")
    sa.write("conversation", conv, "both.md", "a's change")
    sb.write("conversation", conv, "b.md", "only b")
    sb.write("conversation", conv, "both.md", "b's change")
    settle(a, b)
    assert sa.head(conv) == sb.head(conv)
    files = sa.files("conversation", conv)
    assert {"a.md", "b.md", "both.md"} <= set(files)
    copies = [f for f in files if f.startswith("both.md.from-")]
    assert len(copies) == 1  # neither change is lost
    seen = {sa.read("conversation", conv, "both.md"), sa.read("conversation", conv, copies[0])}
    assert seen == {b"a's change", b"b's change"}
    assert sa.log("conversation", conv)[0].message == "merge from another device"


def test_history_with_a_symlink_is_refused(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "data")
    src = tmp_path / "src"
    src.mkdir()
    bad = ArtifactStore(tmp_path / "other")
    bad._ensure(src)
    (src / "link").symlink_to("/etc/passwd")
    bad._git(src, "add", "link")
    bad._git(src, "commit", "--quiet", "-m", "x")
    with pytest.raises(ArtifactError, match="other than files"):
        store.merge_in("11111111-1111-1111-1111-111111111111", src)
    assert not (
        store.repo("conversation", "11111111-1111-1111-1111-111111111111") / "link"
    ).exists()


def test_leaving_keeps_chats_and_a_new_folder_publishes_them_again(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    a, b = pair(tmp_path, folder, "a", "b")
    with a.vault() as v:
        conv = v.create_conversation(title="Moves")
        v.add_message(conv, {"role": "user", "content": "one"})
    settle(a, b)
    # b was lost: a starts again with a new key that b doesn't have
    replication.leave(a.cfg)
    assert replication.status(a.cfg)["configured"] is False
    with a.vault() as v:
        assert v.conversation(conv).title == "Moves"
    second = tmp_path / "second"
    key = replication.init(a.cfg, second, mode="on_demand")
    settle(a)
    c = Device(tmp_path, "c")
    replication.join(c.cfg, second, key, mode="on_demand")
    settle(c)
    with c.vault() as v:
        assert v.messages(conv)[0].body["content"] == "one"
    assert second.name in str(a.cfg.main.replication.folder)


def _deliver(source: Path, target: Path, rng: random.Random, share: float) -> None:
    """Copy some of the changed files, one at a time, like a folder sync partway through."""
    files = [f for f in source.rglob("*") if f.is_file()]
    rng.shuffle(files)
    for f in files[: max(1, int(len(files) * share))]:
        dest = target / f.relative_to(source)
        if not dest.exists() or dest.stat().st_size != f.stat().st_size:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(f, dest)


def test_files_arriving_in_any_order(tmp_path: Path) -> None:
    """Each device has its own copy of the folder, as with Syncthing. The other device's copy
    fills up a few files at a time, in a random order."""
    near = tmp_path / "near"
    a = Device(tmp_path, "a")
    key = replication.init(a.cfg, near, mode="on_demand")
    far = tmp_path / "far"
    far.mkdir()
    shutil.copy(near / replication.MARKER, far / replication.MARKER)
    (far / "devices").mkdir()
    b = Device(tmp_path, "b")
    replication.join(b.cfg, far, key, mode="on_demand")
    me = replication.load_identity(b.cfg)
    assert me is not None
    me.devices[a.id] = "approved"
    replication._save_identity(b.cfg, me)
    with a.vault() as v:
        f = v.create_folder("Work")
        for i in range(6):
            conv = v.create_conversation(title=f"Conversation {i}", folder_id=f)
            parent = None
            for j in range(4):
                parent = v.add_message(conv, {"role": "user", "content": f"{i}.{j}"},
                                       parent_id=parent)  # fmt: skip
    a.sync()
    rng = random.Random(4)  # noqa: S311
    source = near / "devices" / a.id
    target = far / "devices" / a.id
    target.mkdir()
    shutil.copy(source / "device.pub", target / "device.pub")
    while True:
        _deliver(source, target, rng, 0.2)
        report = b.sync()
        assert report["problems"] == []
        have = {f.relative_to(target) for f in target.rglob("*") if f.is_file()}
        if have >= {f.relative_to(source) for f in source.rglob("*") if f.is_file()}:
            break
    b.sync()
    with a.vault() as va, b.vault() as vb:
        for c in va.list_conversations():
            assert [m.body for m in va.messages(c.id)] == [m.body for m in vb.messages(c.id)]
            assert vb.conversation(c.id).folder_id == f
        assert len(vb.list_conversations()) == 6


def test_the_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                      monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    folder = tmp_path / "shared"
    a, b = Device(tmp_path, "a"), Device(tmp_path, "b")

    def run(d: Device, *args: str) -> int:
        return main(["--config-dir", str(d.paths.config_dir), "replicate", *args])

    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(a.paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(a.paths.state_dir))
    assert run(a, "status") == 0
    assert "not replicating" in capsys.readouterr().out
    assert run(a, "init", str(folder), "--name", "laptop", "--mode", "on_demand") == 0
    out = capsys.readouterr().out
    key = next(w for w in out.split() if w.startswith("cw-sync-"))
    assert 'mode = "on_demand"' in a.paths.main_config.read_text()
    assert key not in a.paths.main_config.read_text()
    assert run(a, "init", str(folder)) == 1  # already set up
    capsys.readouterr()

    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(b.paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(b.paths.state_dir))
    monkeypatch.setattr("getpass.getpass", lambda _p: key)
    assert run(b, "join", str(folder), "--mode", "on_demand") == 0
    assert "replicate approve" in capsys.readouterr().out
    assert run(b, "sync") == 0
    identity = b.paths.state_dir / "replication" / "identity.json"
    assert identity.stat().st_mode & 0o777 == 0o600

    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(a.paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(a.paths.state_dir))
    capsys.readouterr()
    assert run(a, "status") == 0
    out = capsys.readouterr().out
    assert "pending" in out and "laptop" in out
    assert key not in out
    bid = replication.load_identity(b.cfg).device  # type: ignore[union-attr]
    assert run(a, "approve", bid[:10]) == 0
    assert run(a, "block", bid[:10]) == 0
    assert "can still read" in capsys.readouterr().out
    assert run(a, "sync") == 0
    assert run(a, "leave") == 0
    assert run(a, "sync") == 1


def _apply_op(devices: list[Device], convs: list[str], op: tuple[str, int, int]) -> None:
    kind, who, which = op
    d = devices[who % len(devices)]
    if kind == "sync":
        d.sync()
        return
    with d.vault() as v:
        live = {c.id for c in v.list_conversations(include_deleted=True)}
        known = [c for c in convs if c in live]
        if kind == "new" or not known:
            convs.append(v.create_conversation(title=f"c{len(convs)}",
                                               crypto_shred=which % 3 != 0))  # fmt: skip
            return
        conv = known[which % len(known)]
        if v.conversation(conv).deleted_at is not None:
            v.restore(conv)
        if kind == "msg":
            msgs = v.messages(conv)
            parent = msgs[which % len(msgs)].id if msgs and which % 2 else (
                msgs[-1].id if msgs else None)  # fmt: skip
            v.add_message(conv, {"role": "user", "content": f"m{which}"}, parent_id=parent)
        elif kind == "pin":
            v.set_pinned(conv, bool(which % 2))
        elif kind == "title":
            v.set_title(conv, f"title {which}")
        elif kind == "trash":
            v.soft_delete(conv)
        elif kind == "purge":
            v.purge(conv, force=True)


OPS = st.tuples(st.sampled_from(["new", "msg", "msg", "msg", "pin", "title", "trash", "purge",
                                 "sync", "sync"]),
                st.integers(0, 2), st.integers(0, 50))  # fmt: skip


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(OPS, min_size=1, max_size=25))
def test_any_order_of_changes_and_syncs_ends_the_same(ops: list[tuple[str, int, int]]) -> None:
    with tempfile.TemporaryDirectory(prefix="cw-") as tmp:
        base = Path(tmp)
        devices = pair(base, base / "s", "a", "b", "c")
        convs: list[str] = []
        for op in ops:
            _apply_op(devices, convs, op)
        settle(*devices)
        first = snapshot(devices[0])
        assert snapshot(devices[1]) == first
        assert snapshot(devices[2]) == first
