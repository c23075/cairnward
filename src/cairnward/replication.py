"""The same chats and artifacts on several devices, through a shared folder (spec §4.8,
FR-SYNC-4, ADR-0007, D72).

The folder is anything every device can reach: Syncthing, a NAS mount, a synced drive.
Cairnward only reads and writes files in it and never talks to the network itself:

    cairnward-sync.json             format, group id and a check value for the sync key
    devices/<id>/device.pub         the device's public signing key; <id> is its hash
    devices/<id>/meta.log           folders, purges, the device's name
    devices/<id>/c/<conversation>.log   the conversation's changes and messages
    devices/<id>/k/<conversation>   its key, replaced when that changes (wrapped when locked)
    devices/<id>/a/<conversation>   its artifacts, replaced when they change

Each device writes only its own folder, so files can't conflict. Every record is encrypted
with the sync key and signed by its device, and is bound to its device, file and position,
so someone who can only change the folder can't read, forge or reorder anything. Message
bodies are also still encrypted with the conversation's own key, so a locked conversation is
never readable with the sync key alone. What the folder's holder can see: how many devices
and conversations there are, when files change, and how large they are.

State is a merge of the logs, and gives the same result in any order:
- messages are a set, ordered by (Lamport clock, device). Two devices replying to the same
  message make a fork in the conversation's message tree; neither reply is lost;
- a conversation's fields (title, pinned, trash...) are per-field, last writer wins;
- a purge always wins, and a purged conversation's files are deleted by each device when it
  next syncs. Until a device has synced, it and its files in the folder still hold the
  conversation, and so does any version history the folder's own software keeps;
- artifacts are git history, merged the same way on every device; a locked conversation's
  are one encrypted file, so the later write wins.

A device's own log is read back like any other, which is also how it recovers from a crash
partway through writing: nothing counts as published until it's been read from the file.

A new device's records aren't accepted until you approve it on an existing device. That's
what blocking relies on too: a lost device still holds the sync key and can read what's in
the folder, so to shut it out properly start a new folder (`leave`, then `init`).

Research libraries, research jobs, settings and the audit log stay on each device.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import Connection, delete, insert, select, update

from cairnward.artifacts import (
    SEALED,
    ArtifactError,
    ArtifactStore,
    _private_tmp,
    _unpack,
    _write_atomic,
)
from cairnward.config.loader import Config
from cairnward.store import Vault, crypto
from cairnward.store.schema import (
    conversation_keys,
    conversations,
    folders,
    messages,
    sync_files,
    sync_items,
    sync_state,
)

Log = Callable[[str], None]
FORMAT = 1
MARKER = "cairnward-sync.json"
MAX_RECORD = 64 * 1024 * 1024
MAX_LAMPORT = 2**53
BATCH = 500
_DEVICE = re.compile(r"^[0-9a-f]{32}$")
_LOG = re.compile(r"^[0-9a-f-]{36}\.log$")
_ITEM = re.compile(r"^[0-9a-f-]{36}$")
_FIELDS = ("title", "environment", "client", "folder_id", "pinned", "archived", "deleted_at",
           "updated_at")  # fmt: skip
_CLOUD = ("dropbox", "google drive", "googledrive", "my drive", "onedrive", "icloud",
          "mobile documents", "nextcloud", "owncloud", "pcloud", "mega")  # fmt: skip


class ReplicationError(Exception):
    pass


class _NotYet(Exception):
    """Depends on something from another file: try again after reading the others."""


class _Bad(Exception):
    """A record that isn't valid: never applied, and reading that file stops there."""


# ------------------------------------------------------------------ records

_Id = Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
_Time = Annotated[str, Field(max_length=32)]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lamport: int = Field(ge=0, le=MAX_LAMPORT)
    origin: str = Field(pattern=r"^[0-9a-f]{32}$")


class _Message(_Event):
    t: Literal["msg"]
    id: _Id
    conv: _Id
    parent: _Id | None
    role: str = Field(min_length=1, max_length=16)
    created_at: _Time
    model: str | None
    effort: str | None
    token_count: int | None
    prefix_hash: str | None = Field(max_length=64)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    enc: Literal["age", "none"]
    body: str


class _Conversation(_Event):
    t: Literal["conv"]
    id: _Id
    created_at: _Time
    enc: Literal["age", "none"]
    fields: dict[str, str | bool | None]


class _Folder(_Event):
    t: Literal["folder"]
    id: _Id
    name: str = Field(min_length=1, max_length=1000)
    parent: _Id | None
    created_at: _Time


class _Tombstone(_Event):
    t: Literal["tomb"]
    id: _Id
    created_at: _Time
    enc: Literal["age", "none"]
    purged_at: _Time


class _DeviceName(_Event):
    t: Literal["device"]
    name: str = Field(max_length=100)


class _Key(_Event):
    t: Literal["key"]
    id: _Id
    kind: Literal["plain", "wrapped"]
    key: str
    recovery: str | None
    created_at: _Time


class _Artifacts(_Event):
    t: Literal["art"]
    id: _Id
    kind: Literal["bundle", "sealed"]
    rev: str = Field(max_length=64)
    data: str


_Any = Annotated[
    _Message | _Conversation | _Folder | _Tombstone | _DeviceName | _Key | _Artifacts,
    Field(discriminator="t"),
]
_PARSE: TypeAdapter[Any] = TypeAdapter(_Any)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def _hash(value: Any) -> str:
    return crypto.digest(json.dumps(value, sort_keys=True).encode())


def _ad(group: str, device: str, rel: str, index: int) -> bytes:
    return f"cairnward-sync-1|{group}|{device}|{rel}|{index}".encode()


def _seal_record(me: Identity, rel: str, index: int, event: dict[str, Any]) -> bytes:
    ad = _ad(me.group, me.device, rel, index)
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(me.sync_key).encrypt(nonce, json.dumps(event).encode(), ad)
    sig = Ed25519PrivateKey.from_private_bytes(me.seed).sign(ad + nonce + ct)
    body = sig + nonce + ct
    if len(body) > MAX_RECORD:
        raise ReplicationError("a record is over the 64 MB limit")
    return len(body).to_bytes(4, "big") + body


def _open_record(me: Identity, pub: bytes, device: str, rel: str, index: int, body: bytes) -> Any:
    """Signature, then decryption, then the event's shape. Events from other devices are
    data: nothing in them is run or passed to a model."""
    if len(body) < 76:
        raise _Bad("too short")
    sig, nonce, ct = body[:64], body[64:76], body[76:]
    ad = _ad(me.group, device, rel, index)
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, ad + nonce + ct)
        data = ChaCha20Poly1305(me.sync_key).decrypt(nonce, ct, ad)
        return _PARSE.validate_python(json.loads(data))
    except InvalidSignature:
        raise _Bad("bad signature") from None
    except InvalidTag:
        raise _Bad("can't decrypt") from None
    except (ValueError, ValidationError):  # the message would echo the content
        raise _Bad("not a valid record") from None


def _frames(data: bytes, pos: int = 0) -> Iterator[tuple[bytes, int]]:
    """(record body, position after it). An incomplete last record just isn't there yet: a
    file still arriving through the folder's own sync, or a crash while writing."""
    while len(data) - pos >= 4:
        n = int.from_bytes(data[pos : pos + 4], "big")
        if n > MAX_RECORD:
            raise _Bad("record too long")
        if len(data) - pos - 4 < n:
            return
        pos += 4 + n
        yield data[pos - n : pos], pos


# ------------------------------------------------------------------ this device


@dataclass
class Identity:
    device: str
    seed: bytes
    sync_key: bytes
    group: str
    name: str
    devices: dict[str, str] = field(default_factory=dict)  # id -> approved | pending | blocked
    names: dict[str, str] = field(default_factory=dict)


def replication_dir(cfg: Config) -> Path:
    return cfg.paths.state_dir / "replication"


def _identity_file(cfg: Config) -> Path:
    return replication_dir(cfg) / "identity.json"


def _last_file(cfg: Config) -> Path:
    return replication_dir(cfg) / "last.json"


def load_identity(cfg: Config) -> Identity | None:
    f = _identity_file(cfg)
    if not f.exists():
        return None
    d = json.loads(f.read_text(encoding="utf-8"))
    return Identity(d["device"], _unb64(d["seed"]), _unb64(d["sync_key"]), d["group"], d["name"],
                    d.get("devices", {}), d.get("names", {}))  # fmt: skip


def _save_identity(cfg: Config, me: Identity) -> None:
    f = _identity_file(cfg)
    f.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(f, json.dumps({
        "device": me.device, "seed": _b64(me.seed), "sync_key": _b64(me.sync_key),
        "group": me.group, "name": me.name, "devices": me.devices, "names": me.names,
    }, indent=1).encode())  # fmt: skip


def _new_identity(group: str, sync_key: bytes, name: str | None) -> Identity:
    seed = secrets.token_bytes(32)
    device = _device_id(_public(seed))
    return Identity(device, seed, sync_key, group, name or f"device-{device[:6]}")


def _public(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)  # fmt: skip


def _device_id(pub: bytes) -> str:
    return hashlib.sha256(pub).hexdigest()[:32]


def format_key(key: bytes) -> str:
    return "cw-sync-" + base64.b32encode(key).decode().rstrip("=").lower()


def parse_key(text: str) -> bytes:
    body = text.strip().lower().removeprefix("cw-sync-").replace("-", "").replace(" ", "")
    try:
        key = base64.b32decode(body.upper() + "=" * (-len(body) % 8))
    except ValueError:
        raise ReplicationError("that isn't a sync key (cw-sync-...)") from None
    if len(key) != 32:
        raise ReplicationError("that isn't a sync key (cw-sync-...)")
    return key


def cloud_warning(folder: Path) -> str | None:
    if any(hint in str(folder).lower() for hint in _CLOUD):
        return ("this looks like a cloud-synced folder: its contents are encrypted, but the "
                "provider sees file names, sizes and when they change, and may keep old versions "
                "of files after a purge")  # fmt: skip
    return None


def _audit(cfg: Config, action: str, outcome: str = "ok", **details: Any) -> None:
    from cairnward.audit import AuditLog, enabled_for

    with contextlib.suppress(Exception), AuditLog.open(cfg.paths, enabled_for(cfg)) as audit:
        audit.append(action, actor="cairnward", outcome=outcome, details=details)


def _marker(folder: Path) -> dict[str, Any] | None:
    f = folder / MARKER
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReplicationError(f"{f} can't be read") from None
    if data.get("format") != FORMAT:
        raise ReplicationError(f"{f} is from another version of Cairnward")
    return dict(data)


def _start_device(cfg: Config, me: Identity, folder: Path) -> None:
    mine = folder / "devices" / me.device
    (mine / "c").mkdir(parents=True, exist_ok=True)
    (mine / "k").mkdir(exist_ok=True)
    (mine / "a").mkdir(exist_ok=True)
    (mine / "device.pub").write_bytes(_public(me.seed))
    me.devices[me.device] = "approved"
    with Vault.open(cfg.paths) as vault:
        _reset_progress(vault)
    _save_identity(cfg, me)


def _reset_progress(vault: Vault) -> None:
    """Everything is published again to a new folder. Messages other devices also hold end up
    with the lowest clock any of them gave it, whatever order the logs are read in."""
    with vault._db.begin() as conn:
        conn.execute(delete(sync_items))
        conn.execute(delete(sync_files))
        conn.execute(delete(sync_state))
        conn.execute(update(messages).values(lamport=None, origin=None))


def init(cfg: Config, folder: Path, *, name: str | None = None, mode: str = "auto") -> str:
    """Start sharing through a new folder; returns the sync key, shown once."""
    from cairnward.config.edit import change_replication

    if load_identity(cfg) is not None:
        raise ReplicationError("this device already replicates (cairnward replicate leave first)")
    folder.mkdir(parents=True, exist_ok=True)
    if _marker(folder) is not None:
        raise ReplicationError(f"{folder} is already in use: cairnward replicate join")
    key = secrets.token_bytes(32)
    group = str(uuid.uuid4())
    nonce = os.urandom(12)
    check = nonce + ChaCha20Poly1305(key).encrypt(nonce, b"cairnward", group.encode())
    marker = {"format": FORMAT, "group": group, "check": _b64(check)}
    _write_atomic(folder / MARKER, json.dumps(marker, indent=1).encode())
    me = _new_identity(group, key, name)
    _start_device(cfg, me, folder)
    change_replication(cfg, mode, str(folder))
    _audit(cfg, "replicate.init", device=me.device)
    return format_key(key)


def join(cfg: Config, folder: Path, key_text: str, *, name: str | None = None,
         mode: str = "auto") -> Identity:  # fmt: skip
    """Join a folder another device set up. The devices already there are trusted: whoever
    gave you the key vouched for them. This device waits for approval on one of them."""
    from cairnward.config.edit import change_replication

    if load_identity(cfg) is not None:
        raise ReplicationError("this device already replicates (cairnward replicate leave first)")
    marker = _marker(folder)
    if marker is None:
        raise ReplicationError(f"{folder} has no {MARKER}: is it the shared folder, and has it "
                               "finished arriving?")  # fmt: skip
    key = parse_key(key_text)
    check = _unb64(marker["check"])
    try:
        ChaCha20Poly1305(key).decrypt(check[:12], check[12:], marker["group"].encode())
    except InvalidTag:
        raise ReplicationError("that sync key doesn't open this folder") from None
    me = _new_identity(marker["group"], key, name)
    for d in _device_dirs(folder):
        me.devices[d.name] = "approved"
    _start_device(cfg, me, folder)
    change_replication(cfg, mode, str(folder))
    _audit(cfg, "replicate.join", device=me.device)
    return me


def leave(cfg: Config) -> None:
    """Stop replicating on this device. Its chats stay here, and what it already put in the
    folder stays there for the other devices."""
    from cairnward.config.edit import change_replication

    if load_identity(cfg) is None:
        raise ReplicationError("this device doesn't replicate")
    with Vault.open(cfg.paths) as vault:
        _reset_progress(vault)
    _identity_file(cfg).unlink()
    _last_file(cfg).unlink(missing_ok=True)
    change_replication(cfg, "none", None)
    _audit(cfg, "replicate.leave")


def _find_device(me: Identity, prefix: str) -> str:
    found = [d for d in me.devices if d.startswith(prefix.lower())]
    if len(found) != 1:
        raise ReplicationError(f"'{prefix}' matches {len(found)} devices (see replicate status)")
    return found[0]


def set_device(cfg: Config, prefix: str, status: Literal["approved", "blocked"]) -> str:
    me = load_identity(cfg)
    if me is None:
        raise ReplicationError("this device doesn't replicate")
    folder = _folder(cfg)
    _note_devices(me, folder)
    device = _find_device(me, prefix)
    if device == me.device:
        raise ReplicationError("that's this device")
    me.devices[device] = status
    _save_identity(cfg, me)
    _audit(cfg, f"replicate.device.{status}", device=device)
    return device


def _folder(cfg: Config) -> Path:
    folder = cfg.main.replication.folder
    if not folder:
        raise ReplicationError("no shared folder set (cairnward replicate init or join)")
    return Path(folder).expanduser()


def _device_dirs(folder: Path) -> list[Path]:
    base = folder / "devices"
    if not base.is_dir():
        return []
    return [d for d in sorted(base.iterdir())
            if _DEVICE.match(d.name) and d.is_dir() and not d.is_symlink()]  # fmt: skip


def _read_public(device_dir: Path) -> bytes | None:
    """The device's key, if it's there and really is the one its name was made from."""
    f = device_dir / "device.pub"
    if f.is_symlink() or not f.is_file():
        return None
    pub = f.read_bytes()
    return pub if len(pub) == 32 and _device_id(pub) == device_dir.name else None


def _note_devices(me: Identity, folder: Path) -> list[str]:
    """Devices seen for the first time wait for approval; returns the new ones."""
    new = []
    for d in _device_dirs(folder):
        if d.name not in me.devices and _read_public(d) is not None:
            me.devices[d.name] = "pending"
            new.append(d.name)
    return new


# ------------------------------------------------------------------ merging


def renumber(conn: Connection, conv_id: str) -> None:
    """Set `seq` from the message tree: depth first, so a fork's messages stay together,
    with children in clock order. Messages not published yet come last, in the order they
    were written."""
    rows = conn.execute(
        select(messages.c.id, messages.c.parent_id, messages.c.lamport, messages.c.origin,
               messages.c.seq).where(messages.c.conversation_id == conv_id)
    ).all()  # fmt: skip
    ids = {r.id for r in rows}
    children: dict[str | None, list[Any]] = {}
    for r in rows:
        children.setdefault(r.parent_id if r.parent_id in ids else None, []).append(r)

    def order(r: Any) -> tuple[bool, int, str, int, str]:
        pending = r.lamport is None
        return (pending, r.lamport or 0, r.origin or "", r.seq if pending else 0, r.id)

    stack = sorted(children.get(None, []), key=order, reverse=True)
    n = 0
    while stack:
        r = stack.pop()
        n += 1
        if r.seq != n:
            conn.execute(update(messages).where(messages.c.id == r.id).values(seq=n))
        stack.extend(sorted(children.get(r.id, []), key=order, reverse=True))


class _Sync:
    def __init__(self, cfg: Config, me: Identity, folder: Path, vault: Vault, log: Log):
        self.cfg, self.me, self.folder, self.vault, self.log = cfg, me, folder, vault, log
        self.store = ArtifactStore(cfg.paths.data_dir, vault)
        self.clock = 0
        self.problems: list[str] = []
        self.waiting: set[str] = set()
        self.counts = {"sent": 0, "received": 0, "purged": 0}

    # ---------------------------------------------------------------- small helpers

    def _load_clock(self) -> None:
        with self.vault._db.connect() as conn:
            row = conn.execute(select(sync_state.c.value).where(sync_state.c.name == "lamport")
                               ).first()  # fmt: skip
        self.clock = int(row.value) if row else 0

    def _save_clock(self, conn: Connection) -> None:
        conn.execute(delete(sync_state).where(sync_state.c.name == "lamport"))
        conn.execute(insert(sync_state).values(name="lamport", value=str(self.clock)))

    def _see(self, event: Any) -> None:
        self.clock = max(self.clock, event.lamport)

    def _tick(self) -> dict[str, Any]:
        self.clock += 1
        return {"lamport": self.clock, "origin": self.me.device}

    def _problem(self, device: str, rel: str, what: str) -> None:
        self.problems.append(f"{device[:10]} {rel}: {what}")

    @staticmethod
    def _state(conn: Connection, kind: str, item: str) -> dict[str, list[Any]]:
        row = conn.execute(select(sync_items.c.state).where(
            sync_items.c.kind == kind, sync_items.c.id == item)).first()  # fmt: skip
        return dict(json.loads(row.state)) if row else {}

    @staticmethod
    def _set_state(conn: Connection, kind: str, item: str, state: dict[str, list[Any]]) -> None:
        conn.execute(delete(sync_items).where(sync_items.c.kind == kind, sync_items.c.id == item))
        conn.execute(insert(sync_items).values(kind=kind, id=item, state=json.dumps(state)))

    @staticmethod
    def _position(conn: Connection, device: str, rel: str) -> tuple[int, int]:
        row = conn.execute(select(sync_files.c.offset, sync_files.c.records).where(
            sync_files.c.device == device, sync_files.c.path == rel)).first()  # fmt: skip
        return (row.offset, row.records) if row else (0, 0)

    @staticmethod
    def _set_position(conn: Connection, device: str, rel: str, offset: int, records: int) -> None:
        conn.execute(delete(sync_files).where(sync_files.c.device == device,
                                              sync_files.c.path == rel))  # fmt: skip
        conn.execute(insert(sync_files).values(device=device, path=rel, offset=offset,
                                               records=records))  # fmt: skip

    def _key_row(self, conv_id: str) -> Any:
        with self.vault._keys.connect() as conn:
            return conn.execute(select(conversation_keys).where(
                conversation_keys.c.conversation_id == conv_id)).first()  # fmt: skip

    # ---------------------------------------------------------------- applying events

    def _apply(self, conn: Connection, device: str, event: Any, touched: set[str]) -> None:
        self._see(event)
        if isinstance(event, _Message):
            self._apply_message(conn, device, event, touched)
        elif isinstance(event, _Conversation):
            self._apply_conversation(conn, event)
        elif isinstance(event, _Folder):
            self._apply_folder(conn, event)
        elif isinstance(event, _DeviceName):
            self.me.names[device] = event.name

    def _apply_message(self, conn: Connection, device: str, e: _Message,
                       touched: set[str]) -> None:  # fmt: skip
        conv = conn.execute(select(conversations.c.enc, conversations.c.purged_at).where(
            conversations.c.id == e.conv)).first()  # fmt: skip
        if conv is None:
            raise _NotYet
        if conv.purged_at is not None:
            return
        if e.enc != conv.enc:
            raise _Bad("message encryption doesn't match its conversation")
        have = conn.execute(select(messages.c.lamport, messages.c.origin).where(
            messages.c.id == e.id)).first()  # fmt: skip
        if have is not None:
            if have.lamport is None or (e.lamport, e.origin) < (have.lamport, have.origin):
                conn.execute(update(messages).where(messages.c.id == e.id).values(
                    lamport=e.lamport, origin=e.origin))  # fmt: skip
                touched.add(e.conv)
            return
        parent = select(messages.c.id).where(messages.c.id == e.parent)
        if e.parent is not None and conn.execute(parent).first() is None:
            raise _NotYet
        try:
            body = _unb64(e.body)
        except ValueError:
            raise _Bad("message body isn't base64") from None
        if e.enc == "none" and crypto.digest(body) != e.content_digest:
            raise _Bad("message failed its integrity check")
        conn.execute(insert(messages).values(
            id=e.id, conversation_id=e.conv, parent_id=e.parent, seq=0, role=e.role,
            created_at=e.created_at, model=e.model, effort=e.effort, token_count=e.token_count,
            prefix_hash=e.prefix_hash, content_digest=e.content_digest, enc=e.enc, body=body,
            lamport=e.lamport, origin=e.origin))  # fmt: skip
        touched.add(e.conv)
        if device != self.me.device:
            self.counts["received"] += 1

    def _apply_conversation(self, conn: Connection, e: _Conversation) -> None:
        if set(e.fields) - set(_FIELDS):
            raise _Bad("unknown conversation field")
        decoded = {name: _decode_field(name, value) for name, value in e.fields.items()}
        row = conn.execute(select(conversations).where(conversations.c.id == e.id)).first()
        if row is not None and row.purged_at is not None:
            return
        if e.enc == "age" and self._key_row(e.id) is None:
            raise _NotYet  # its key comes first: a row without one can't be read
        folder = e.fields.get("folder_id")
        if folder is not None and conn.execute(
                select(folders.c.id).where(folders.c.id == folder)).first() is None:  # fmt: skip
            raise _NotYet
        if row is None:
            conn.execute(insert(conversations).values(
                id=e.id, created_at=e.created_at, updated_at=e.created_at, enc=e.enc,
                pinned=False, archived=False))  # fmt: skip
            row = conn.execute(select(conversations).where(conversations.c.id == e.id)).first()
        state = self._state(conn, "conv", e.id)
        changes: dict[str, Any] = {}
        for name, value in e.fields.items():
            old = state.get(name)
            if old is not None and (e.lamport, e.origin) <= (old[0], old[1]):
                continue
            # A local change not published yet goes out next with a later clock, and wins.
            if old is None or _field_hash(row, name) == old[2]:
                changes[name] = decoded[name]
            state[name] = [e.lamport, e.origin, _hash(value)]
        if changes:
            conn.execute(update(conversations).where(conversations.c.id == e.id).values(**changes))
        self._set_state(conn, "conv", e.id, state)

    def _apply_folder(self, conn: Connection, e: _Folder) -> None:
        if conn.execute(select(folders.c.id).where(folders.c.id == e.id)).first() is None:
            parent = select(folders.c.id).where(folders.c.id == e.parent)
            if e.parent is not None and conn.execute(parent).first() is None:
                raise _NotYet
            conn.execute(insert(folders).values(id=e.id, name=e.name, parent_id=e.parent,
                                                created_at=e.created_at))  # fmt: skip
        self._set_state(conn, "folder", e.id, {})

    def _apply_tombstone(self, e: _Tombstone) -> None:
        """A purge always wins. Runs outside the batch: `purge` has its own transactions."""
        self._see(e)
        with self.vault._db.connect() as conn:
            row = conn.execute(select(conversations.c.purged_at).where(
                conversations.c.id == e.id)).first()  # fmt: skip
        if row is not None and row.purged_at is None:
            self.vault.purge(e.id, force=True)
            self.counts["purged"] += 1
            _audit(self.cfg, "replicate.purge", conversation=e.id)
        with self.vault._db.begin() as conn:
            if row is None:
                conn.execute(insert(conversations).values(
                    id=e.id, created_at=e.created_at, updated_at=e.purged_at, enc=e.enc,
                    pinned=False, archived=False, deleted_at=e.purged_at,
                    purged_at=e.purged_at))  # fmt: skip
            else:  # the earliest time, so every device ends up with the same row
                conn.execute(update(conversations).where(
                    conversations.c.id == e.id,
                    conversations.c.purged_at >= e.purged_at,
                ).values(purged_at=e.purged_at, deleted_at=e.purged_at,
                         updated_at=e.purged_at))  # fmt: skip
            kinds = ("conv", "key", "art")
            conn.execute(delete(sync_items).where(sync_items.c.id == e.id,
                                                  sync_items.c.kind.in_(kinds)))  # fmt: skip
            conn.execute(delete(sync_files).where(sync_files.c.path.in_(
                (f"c/{e.id}.log", f"k/{e.id}", f"a/{e.id}"))))  # fmt: skip
            self._set_state(conn, "tomb", e.id, {})
        self._remove_own_files(e.id)

    def _remove_own_files(self, conv_id: str) -> None:
        mine = self.folder / "devices" / self.me.device
        for rel in (f"c/{conv_id}.log", f"k/{conv_id}", f"a/{conv_id}"):
            (mine / rel).unlink(missing_ok=True)

    def _apply_key(self, e: _Key) -> None:
        self._see(e)
        clock = (e.lamport, e.origin)
        with self.vault._db.begin() as conn:
            if conn.execute(select(sync_items.c.id).where(
                    sync_items.c.kind == "tomb", sync_items.c.id == e.id)).first():  # fmt: skip
                return
            old = self._state(conn, "key", e.id).get("_")
            if old is not None and clock <= (old[0], old[1]):
                return
            key, recovery = _unb64(e.key), _unb64(e.recovery) if e.recovery else None
            have = self._key_row(e.id)
            digest = _key_hash(e.kind, key, recovery)
            current = _key_hash(have.kind, bytes(have.key), have.recovery) if have else None
            if have is None or old is None or current == old[2]:
                self._follow_lock(e.id, have, e.kind, key)
                self._store_key(e, have, key, recovery)
            self._set_state(conn, "key", e.id, {"_": [e.lamport, e.origin, digest]})
        if e.origin != self.me.device:  # an older copy of the key shouldn't stay in the folder
            (self.folder / "devices" / self.me.device / "k" / e.id).unlink(missing_ok=True)

    def _store_key(self, e: _Key, have: Any, key: bytes, recovery: bytes | None) -> None:
        with self.vault._keys.begin() as kconn:
            if have is None:
                kconn.execute(insert(conversation_keys).values(
                    conversation_id=e.id, kind=e.kind, key=key, recovery=recovery,
                    created_at=e.created_at))  # fmt: skip
            else:
                kconn.execute(update(conversation_keys).where(
                    conversation_keys.c.conversation_id == e.id
                ).values(kind=e.kind, key=key, recovery=recovery))  # fmt: skip

    def _follow_lock(self, conv_id: str, have: Any, kind: str, key: bytes) -> None:
        """Locked or unlocked on another device: this device's artifacts follow, before the
        plain key is overwritten."""
        if have is None or have.kind == kind:
            return
        if kind == "wrapped":
            self.store.seal(conv_id, bytes(have.key))
        else:
            self.store.unseal(conv_id, key)

    def _apply_artifacts(self, e: _Artifacts) -> None:
        self._see(e)
        with self.vault._db.connect() as conn:
            conv = conn.execute(select(conversations.c.enc, conversations.c.purged_at).where(
                conversations.c.id == e.id)).first()  # fmt: skip
        if conv is None:
            raise _NotYet
        if conv.purged_at is not None:
            return
        have = self._key_row(e.id)
        if conv.enc == "age" and have is None:
            raise _NotYet
        locked = have is not None and have.kind == "wrapped"
        data = _unb64(e.data)
        if e.kind == "sealed" and locked:  # can't be opened here: the later write wins
            with self.vault._db.begin() as conn:
                old = self._state(conn, "art", e.id).get("_")
                if old is not None and (e.lamport, e.origin) <= (old[0], old[1]):
                    return
                repo = self.store.repo("conversation", e.id)
                repo.mkdir(parents=True, exist_ok=True)
                _write_atomic(repo / SEALED, data)
                self._set_state(conn, "art", e.id, {"_": [e.lamport, e.origin, e.rev]})
            return
        if locked:
            raise _NotYet  # a plain repository's history, and the key is locked here
        if conv.enc == "age":
            data = crypto.open_sealed(bytes(have.key), data)
        with tempfile.TemporaryDirectory(prefix="cw-rep-", dir=_private_tmp()) as tmp:
            if e.kind == "sealed":
                source = Path(tmp) / "repo"
                source.mkdir()
                _unpack(data, source)
            else:
                source = Path(tmp) / "history.bundle"
                source.write_bytes(data)
            head = self.store.merge_in(e.id, source)
        with self.vault._db.begin() as conn:
            if head == e.rev:  # otherwise merged: this device's result still has to go out
                self._set_state(conn, "art", e.id, {"_": [e.lamport, e.origin, head]})

    # ---------------------------------------------------------------- reading files

    def read_log(self, device: str, pub: bytes, rel: str) -> int:
        """Apply what's new in one log; returns how many records."""
        path = self.folder / "devices" / device / rel
        if path.is_symlink() or not path.is_file():
            return 0
        with self.vault._db.connect() as conn:
            offset, index = self._position(conn, device, rel)
        size = path.stat().st_size
        if size < offset:
            self._problem(device, rel, "shorter than when last read: something in the shared "
                                       "folder removed records")  # fmt: skip
            return 0
        if size == offset:
            return 0
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(2 * MAX_RECORD)  # a huge file can't fill the memory: more next pass
        applied, stop = 0, False
        batch: list[tuple[Any, int]] = []
        try:
            for body, end in _frames(data):
                event = _open_record(self.me, pub, device, rel, index + len(batch), body)
                batch.append((event, end))
                if isinstance(event, _Tombstone) or len(batch) >= BATCH:
                    n, stop = self._apply_batch(device, rel, batch, offset, index)
                    applied, index, batch = applied + n, index + n, []
                    if stop:
                        break
        except _Bad as exc:
            self._problem(device, rel, f"stopped reading: {exc}")
        if batch and not stop:
            n, stop = self._apply_batch(device, rel, batch, offset, index)
            applied += n
        return applied

    def _apply_batch(self, device: str, rel: str, batch: list[tuple[Any, int]], base: int,
                     index: int) -> tuple[int, bool]:  # fmt: skip
        """Returns (records applied, whether to stop). A record that can't be applied leaves
        nothing behind: each checks everything before it writes."""
        done, pos, stop = 0, 0, False
        tomb = batch[-1][0] if isinstance(batch[-1][0], _Tombstone) else None
        plain = batch[:-1] if tomb else batch
        touched: set[str] = set()
        with self.vault._db.begin() as conn:
            for event, end in plain:
                try:
                    self._apply(conn, device, event, touched)
                except _NotYet:
                    self.waiting.add(f"{device[:10]} {rel}")
                    stop = True
                    break
                except _Bad as exc:
                    self._problem(device, rel, f"stopped reading: {exc}")
                    stop = True
                    break
                done, pos = done + 1, end
            for conv in touched:
                renumber(conn, conv)
            if done:
                self._set_position(conn, device, rel, base + pos, index + done)
            self._save_clock(conn)
        if tomb is not None and not stop:
            self._apply_tombstone(tomb)
            done, pos = done + 1, batch[-1][1]
            with self.vault._db.begin() as conn:
                self._set_position(conn, device, rel, base + pos, index + done)
                self._save_clock(conn)
        return done, stop

    def read_file(self, device: str, pub: bytes, rel: str) -> int:
        """A key or artifacts file, replaced whole. Read when it changes."""
        path = self.folder / "devices" / device / rel
        if path.is_symlink() or not path.is_file():
            return 0
        st = path.stat()
        with self.vault._db.connect() as conn:
            seen = self._position(conn, device, rel)
        if seen == (st.st_size, st.st_mtime_ns):
            return 0
        if st.st_size > MAX_RECORD + 4:
            self._problem(device, rel, "too large")
            return 0
        try:
            for body, _end in _frames(path.read_bytes()):
                event = _open_record(self.me, pub, device, rel, 0, body)
                if isinstance(event, _Key):
                    self._apply_key(event)
                elif isinstance(event, _Artifacts):
                    if not self.cfg.main.replication.include_artifacts:
                        return 0
                    self._apply_artifacts(event)
                else:
                    raise _Bad("not the kind of record this file holds")
                with self.vault._db.begin() as conn:
                    self._set_position(conn, device, rel, st.st_size, st.st_mtime_ns)
                    self._save_clock(conn)
                return 1
        except _NotYet:
            self.waiting.add(f"{device[:10]} {rel}")
        except (_Bad, ValueError, crypto.DecryptionError, ArtifactError) as exc:
            detail = str(exc) if isinstance(exc, _Bad) else "can't be applied"
            self._problem(device, rel, detail)
        return 0

    def read_all(self) -> None:
        """Every approved device, repeatedly: logs can arrive in any order, and a record that
        depends on another file waits for it."""
        for new in _note_devices(self.me, self.folder):
            self.log(f"replicate: new device {new[:10]} is waiting for approval "
                     f"(cairnward replicate approve {new[:10]})")  # fmt: skip
        sources = []
        for d in _device_dirs(self.folder):
            if self.me.devices.get(d.name) != "approved":
                continue
            pub = _read_public(d)
            if pub is None:
                self._problem(d.name, "device.pub", "missing, or not this device's key")
                continue
            sources.append((d, pub))
        progress = True
        while progress:
            progress = False
            self.waiting.clear()
            for d, pub in sources:
                progress = self.read_log(d.name, pub, "meta.log") > 0 or progress
            for d, pub in sources:
                for rel in _names(d, "k", _ITEM):
                    progress = self.read_file(d.name, pub, rel) > 0 or progress
            for d, pub in sources:
                for rel in _names(d, "c", _LOG):
                    progress = self.read_log(d.name, pub, rel) > 0 or progress
            for d, pub in sources:
                for rel in _names(d, "a", _ITEM):
                    progress = self.read_file(d.name, pub, rel) > 0 or progress

    # ---------------------------------------------------------------- writing this device's

    def _append(self, rel: str, events: list[dict[str, Any]]) -> None:
        """Add to this device's log and read it back. A half-written record from a crash is
        cut off first."""
        path = self.folder / "devices" / self.me.device / rel
        with self.vault._db.connect() as conn:
            offset, index = self._position(conn, self.me.device, rel)
        size = path.stat().st_size if path.exists() else 0
        if size < offset:
            raise ReplicationError(f"this device's {rel} in the shared folder is shorter than it "
                                   "was: something removed records. To start again: cairnward "
                                   "replicate leave, then init on a new folder")  # fmt: skip
        data = b"".join(_seal_record(self.me, rel, index + i, e) for i, e in enumerate(events))
        with path.open("ab") as fh:
            if size > offset:
                fh.truncate(offset)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        self.read_log(self.me.device, _public(self.me.seed), rel)
        self.counts["sent"] += len(events)

    def _replace(self, rel: str, event: dict[str, Any]) -> None:
        path = self.folder / "devices" / self.me.device / rel
        _write_atomic(path, _seal_record(self.me, rel, 0, event))
        self.read_file(self.me.device, _public(self.me.seed), rel)
        self.counts["sent"] += 1

    def export(self) -> None:
        """Publish what changed here since last time. Found by comparing, so every way of
        changing the vault is covered, however it was made."""
        rep = self.cfg.main.replication
        mine = self.folder / "devices" / self.me.device
        with self.vault._db.connect() as conn:
            items = conn.execute(select(sync_items.c.kind, sync_items.c.id))
            known = {(r.kind, r.id) for r in items}
            folder_rows = conn.execute(select(folders)).all()
            conv_rows = conn.execute(select(conversations)).all()
        meta: list[dict[str, Any]] = []
        if not (mine / "meta.log").exists():
            meta.append({"t": "device", "name": self.me.name, **self._tick()})
        for f in folder_rows:
            if ("folder", f.id) not in known:
                meta.append({"t": "folder", "id": f.id, "name": f.name, "parent": f.parent_id,
                             "created_at": f.created_at, **self._tick()})  # fmt: skip
        for c in conv_rows:
            if c.purged_at is not None and ("tomb", c.id) not in known:
                meta.append({"t": "tomb", "id": c.id, "created_at": c.created_at, "enc": c.enc,
                             "purged_at": c.purged_at, **self._tick()})  # fmt: skip
        if meta:
            self._append("meta.log", meta)
        for c in conv_rows:
            if c.purged_at is None and c.environment not in rep.exclude_environments:
                self._export_conversation(c)
                if rep.include_artifacts:
                    try:
                        self._export_artifacts(c)
                    except (ArtifactError, ReplicationError) as exc:
                        self._problem(self.me.device, f"a/{c.id}", str(exc))

    def _export_conversation(self, c: Any) -> None:
        if c.enc == "age":
            self._export_key(c.id)
        rel = f"c/{c.id}.log"
        with self.vault._db.connect() as conn:
            state = self._state(conn, "conv", c.id)
            new = conn.execute(select(messages).where(
                messages.c.conversation_id == c.id, messages.c.lamport.is_(None)
            ).order_by(messages.c.seq)).all()  # fmt: skip
        changed = {name: _encode_field(name, getattr(c, name)) for name in _FIELDS
                   if name not in state or _field_hash(c, name) != state[name][2]}  # fmt: skip
        events: list[dict[str, Any]] = []
        if changed:
            events.append({"t": "conv", "id": c.id, "created_at": c.created_at, "enc": c.enc,
                           "fields": changed, **self._tick()})  # fmt: skip
        for m in new:
            events.append({"t": "msg", "id": m.id, "conv": c.id, "parent": m.parent_id,
                           "role": m.role, "created_at": m.created_at, "model": m.model,
                           "effort": m.effort, "token_count": m.token_count,
                           "prefix_hash": m.prefix_hash, "content_digest": m.content_digest,
                           "enc": m.enc, "body": _b64(bytes(m.body)), **self._tick()})  # fmt: skip
        if events:
            self._append(rel, events)

    def _export_key(self, conv_id: str) -> None:
        row = self._key_row(conv_id)
        if row is None:
            return
        digest = _key_hash(row.kind, bytes(row.key), row.recovery)
        with self.vault._db.connect() as conn:
            old = self._state(conn, "key", conv_id).get("_")
        if old is None or old[2] != digest:
            self._replace(f"k/{conv_id}", {
                "t": "key", "id": conv_id, "kind": row.kind, "key": _b64(bytes(row.key)),
                "recovery": _b64(bytes(row.recovery)) if row.recovery else None,
                "created_at": row.created_at, **self._tick()})  # fmt: skip

    def _export_artifacts(self, c: Any) -> None:
        repo = self.store.repo("conversation", c.id)
        if not repo.is_dir():
            return
        sealed = repo / SEALED
        row = self._key_row(c.id)
        if c.enc == "age" and row is None:
            return
        if sealed.exists():
            data, kind = sealed.read_bytes(), "sealed"
            rev = crypto.digest(data)
        elif row is not None and row.kind == "wrapped":
            return
        else:
            rev = self.store.head(c.id) or ""
            if not rev:
                return
            kind = "bundle"
            data = b""
        with self.vault._db.connect() as conn:
            old = self._state(conn, "art", c.id).get("_")
        if old is not None and old[2] == rev:
            return
        if kind == "bundle":
            data = self.store.bundle(c.id)
            if c.enc == "age":
                data = crypto.seal(bytes(row.key), data)
        self._replace(f"a/{c.id}", {"t": "art", "id": c.id, "kind": kind, "rev": rev,
                                    "data": _b64(data), **self._tick()})  # fmt: skip


def _names(device_dir: Path, sub: str, pattern: re.Pattern[str]) -> list[str]:
    base = device_dir / sub
    if not base.is_dir():
        return []
    return [f"{sub}/{f.name}" for f in sorted(base.iterdir()) if pattern.match(f.name)]


def _encode_field(name: str, value: Any) -> str | bool | None:
    if name == "title":
        return _b64(bytes(value)) if value is not None else None
    if name in ("pinned", "archived"):
        return bool(value)
    return str(value) if value is not None else None


def _decode_field(name: str, value: Any) -> Any:
    if name == "title" and value is not None:
        if not isinstance(value, str):
            raise _Bad("title isn't text")
        try:
            return _unb64(value)
        except ValueError:
            raise _Bad("title isn't base64") from None
    if name in ("pinned", "archived") and not isinstance(value, bool):
        raise _Bad(f"{name} isn't true or false")
    if name == "updated_at" and not isinstance(value, str):
        raise _Bad("updated_at isn't text")
    if name not in ("title", "pinned", "archived") and isinstance(value, bool):
        raise _Bad(f"{name} isn't text")
    return value


def _field_hash(row: Any, name: str) -> str:
    return _hash(_encode_field(name, getattr(row, name)))


def _key_hash(kind: str, key: bytes, recovery: Any) -> str:
    return _hash([kind, _b64(key), _b64(bytes(recovery)) if recovery else None])


# ------------------------------------------------------------------ running


@contextmanager
def _locked(cfg: Config) -> Iterator[None]:
    d = replication_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    fh = (d / "lock").open("w")
    try:
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # Windows: no lock (replication is Linux-first)
            pass
        except BlockingIOError:
            raise ReplicationError("another sync is running") from None
        yield
    finally:
        fh.close()


def sync(cfg: Config, log: Log = lambda _m: None) -> dict[str, Any]:
    """Read every approved device, then publish what's new here."""
    me = load_identity(cfg)
    if me is None:
        raise ReplicationError("this device doesn't replicate (cairnward replicate init or join)")
    folder = _folder(cfg)
    marker = _marker(folder) if folder.is_dir() else None
    # An unplugged disk looks like an empty folder: nothing is written to it.
    if marker is None:
        raise ReplicationError(f"the shared folder {folder} isn't there (not mounted?)")
    if marker.get("group") != me.group:
        raise ReplicationError(f"{folder} belongs to another group of devices")
    own = folder / "devices" / me.device
    if any(p.is_symlink() for p in (own, own / "c", own / "k", own / "a")):
        raise ReplicationError(f"this device's folder in {folder} has been replaced by a link")
    if not own.is_dir():
        raise ReplicationError(f"this device's folder is missing from {folder}. To start again: "
                               "cairnward replicate leave, then init or join")  # fmt: skip
    start = time.monotonic()
    with _locked(cfg), Vault.open(cfg.paths) as vault:
        run = _Sync(cfg, me, folder, vault, log)
        run._load_clock()
        run.read_all()
        run.export()
        _save_identity(cfg, me)
    report: dict[str, Any] = {
        **run.counts, "seconds": round(time.monotonic() - start, 2),
        "time": datetime.now(UTC).isoformat(timespec="seconds"),
        "waiting": sorted(run.waiting), "problems": run.problems[:50],
        "pending_devices": [d for d, s in me.devices.items() if s == "pending"],
    }  # fmt: skip
    _write_atomic(_last_file(cfg), json.dumps(report).encode())
    if run.counts["sent"] or run.counts["received"] or run.counts["purged"] or run.problems:
        _audit(cfg, "replicate.sync", "error" if run.problems else "ok", **run.counts,
               problems=len(run.problems))  # fmt: skip
    return report


def status(cfg: Config, *, check_folder: bool = True) -> dict[str, Any]:
    """For the CLI, doctor and dashboard. Holds no keys or content. The isolated core can't
    reach the shared folder, so its dashboard doesn't look."""
    rep = cfg.main.replication
    me = load_identity(cfg)
    last = None
    with contextlib.suppress(OSError, ValueError):
        last = json.loads(_last_file(cfg).read_text(encoding="utf-8"))
    out: dict[str, Any] = {
        "mode": rep.mode, "folder": rep.folder, "configured": me is not None, "last": last,
        "devices": [], "problem": None, "cloud_warning": None,
    }  # fmt: skip
    if me is None:
        return out
    out["device"] = me.device
    out["name"] = me.name
    try:
        folder = _folder(cfg)
        out["cloud_warning"] = cloud_warning(folder)
        if not check_folder:
            pass
        elif _marker(folder) is None:
            out["problem"] = f"the shared folder {folder} isn't there (not mounted?)"
        else:
            _note_devices(me, folder)
    except ReplicationError as exc:
        out["problem"] = str(exc)
    names = {**me.names, me.device: me.name}
    out["devices"] = [{"id": d, "name": names.get(d, ""), "status": s, "this": d == me.device}
                      for d, s in sorted(me.devices.items())]  # fmt: skip
    return out
