"""Hash-chained, append-only audit log (spec §5.2, ADR-0008).

Each record's `hash` is the SHA-256 of its canonical JSON (all fields except `hash`,
including `prev_hash`), so changing, removing or reordering any record breaks the chain
from that point on. Records are stored in audit.db and mirrored line by line to
audit.jsonl; `verify()` checks the chain and that both copies agree.

Records hold metadata and digests only. Content lives in the vault and is referenced
by digest; `details` is checked for content-like keys and long values.

What the chain alone can't detect: someone with write access rewriting *every* record
after a point (recomputing all hashes), or cutting records off the end. Comparing with
a head noted elsewhere (`audit head`, `verify --expect`) catches both; automatic git
anchoring follows in M2 (FR-AUD-4).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from sqlalchemy import Connection, Engine, insert, select

from cairnward.config.paths import Paths
from cairnward.store.db import make_engine, upgrade
from cairnward.store.schema import audit_records

GENESIS = "0" * 64
Phase = Literal["pre", "post", "event"]
Decision = Literal["allow", "ask", "deny"]

# Records about the audit log itself are written even where auditing is switched off.
ALWAYS_RECORDED = {"settings.snapshot", "settings.changed", "audit.disabled", "audit.enabled"}
# Keys that suggest content or secrets ended up in `details`.
FORBIDDEN_DETAIL_KEYS = {
    "content", "prompt", "body", "messages", "message", "text", "passphrase",
    "password", "api_key", "apikey", "secret", "key", "private_key",
}  # fmt: skip
MAX_DETAIL_STRING = 512


class AuditError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(data: bytes | str) -> str:
    """SHA-256 hex digest, for params_digest/result_digest."""
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _check_details(value: Any, path: str = "details") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in FORBIDDEN_DETAIL_KEYS:
                raise AuditError(
                    f"{path}.{k}: audit records hold metadata only; store content in the vault "
                    "and record its digest"
                )
            _check_details(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _check_details(v, f"{path}[{i}]")
    elif isinstance(value, str) and len(value) > MAX_DETAIL_STRING:
        raise AuditError(f"{path}: string longer than {MAX_DETAIL_STRING} characters")


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: str
    actor: str
    action: str
    phase: Phase
    target: str | None
    decision: Decision | None
    outcome: str | None
    environment: str | None
    client: str | None
    run_id: str | None
    conversation_id: str | None
    pre_seq: int | None
    params_digest: str | None
    result_digest: str | None
    details: dict[str, Any]
    prev_hash: str
    hash: str

    def hashed_fields(self) -> dict[str, Any]:
        data = asdict(self)
        del data["hash"]
        return data

    def compute_hash(self) -> str:
        return digest(canonical(self.hashed_fields()))

    def to_json_line(self) -> str:
        return canonical(asdict(self))


@dataclass
class VerifyResult:
    ok: bool
    records: int
    head_seq: int
    head_hash: str
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _row_to_record(row: Any) -> AuditRecord:
    return AuditRecord(
        seq=row.seq,
        ts=row.ts,
        actor=row.actor,
        action=row.action,
        phase=row.phase,
        target=row.target,
        decision=row.decision,
        outcome=row.outcome,
        environment=row.environment,
        client=row.client,
        run_id=row.run_id,
        conversation_id=row.conversation_id,
        pre_seq=row.pre_seq,
        params_digest=row.params_digest,
        result_digest=row.result_digest,
        details=json.loads(row.details),
        prev_hash=row.prev_hash,
        hash=row.hash,
    )


class Effect:
    """Handle returned by `AuditLog.effect()`; set the result digest before the block ends."""

    def __init__(self) -> None:
        self.result_digest: str | None = None
        self.details: dict[str, Any] = {}

    def result(self, data: bytes | str | None = None, **details: Any) -> None:
        if data is not None:
            self.result_digest = digest(data)
        self.details.update(details)


class AuditLog:
    """Open with `AuditLog.open(paths)`. `enabled_for(environment)` says whether actions in an
    environment are recorded (privacy setting `audit`); records in ALWAYS_RECORDED always are."""

    def __init__(
        self,
        engine: Engine,
        mirror: Path | None,
        enabled_for: Callable[[str | None], bool] | None = None,
    ):
        self._engine = engine
        self.mirror = mirror
        self.enabled_for = enabled_for or (lambda _env: True)
        self._writer = engine.execution_options(isolation_level="AUTOCOMMIT")
        # Appenders in this process queue here, without a time limit, before SQLite's lock: a
        # slow fsync under load once made them wait past busy_timeout and fail with "database is
        # locked", dropping the connection they were recording (found by bench_egress.py).
        self._appending = threading.Lock()

    @classmethod
    def open(cls, paths: Paths, enabled_for: Callable[[str | None], bool] | None = None) -> Self:
        from cairnward.store import dbkey

        key = dbkey.key_for(paths)
        engine = make_engine(paths.data_dir / "audit.db", key)
        upgrade(engine, "audit")
        # An encrypted log keeps no plaintext mirror: it would hold everything the encryption
        # hides (ADR-0019). Export with `audit log --json` instead.
        log = cls(engine, None if key else paths.data_dir / "audit.jsonl", enabled_for)
        log.sync_mirror()
        return log

    def close(self) -> None:
        self._engine.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ writing

    @contextmanager
    def _write_lock(self) -> Iterator[Connection]:
        """One appender at a time: a lock within this process, then SQLite's write lock across
        processes (BEGIN IMMEDIATE; the outer process and the isolated core share the file)."""
        with self._appending, self._writer.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.exec_driver_sql("ROLLBACK")
                raise
            conn.exec_driver_sql("COMMIT")

    def append(
        self,
        action: str,
        *,
        actor: str = "cairnward",
        phase: Phase = "event",
        target: str | None = None,
        decision: Decision | None = None,
        outcome: str | None = None,
        environment: str | None = None,
        client: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        pre_seq: int | None = None,
        params_digest: str | None = None,
        result_digest: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditRecord | None:
        """Append one record. Returns None when auditing is off for `environment`."""
        if action not in ALWAYS_RECORDED and not self.enabled_for(environment):
            return None
        details = details or {}
        _check_details(details)
        with self._write_lock() as conn:
            self._sync_mirror_locked(conn)
            last = conn.execute(
                select(audit_records.c.seq, audit_records.c.hash)
                .order_by(audit_records.c.seq.desc())
                .limit(1)
            ).first()
            seq, prev = (last.seq + 1, last.hash) if last else (1, GENESIS)
            unhashed = AuditRecord(
                seq=seq, ts=_now(), actor=actor, action=action, phase=phase, target=target,
                decision=decision, outcome=outcome, environment=environment, client=client,
                run_id=run_id, conversation_id=conversation_id, pre_seq=pre_seq,
                params_digest=params_digest, result_digest=result_digest,
                details=json.loads(canonical(details)), prev_hash=prev, hash="",
            )  # fmt: skip
            record = AuditRecord(**{**asdict(unhashed), "hash": unhashed.compute_hash()})
            values = asdict(record)
            values["details"] = canonical(record.details)
            conn.execute(insert(audit_records).values(**values))
            self._append_mirror([record])
        return record

    @contextmanager
    def effect(
        self,
        action: str,
        *,
        actor: str,
        target: str | None = None,
        decision: Decision = "allow",
        params: bytes | str | None = None,
        environment: str | None = None,
        client: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Iterator[Effect]:
        """Record `pre` before the block runs and `post` after it, with outcome ok/error."""
        common: dict[str, Any] = dict(
            actor=actor, target=target, environment=environment, client=client,
            run_id=run_id, conversation_id=conversation_id,
        )  # fmt: skip
        pre = self.append(
            action, phase="pre", decision=decision,
            params_digest=digest(params) if params is not None else None,
            details=details, **common,
        )  # fmt: skip
        eff = Effect()
        try:
            yield eff
        except BaseException as exc:
            self.append(
                action, phase="post", outcome="error", pre_seq=pre.seq if pre else None,
                details={**eff.details, "error": type(exc).__name__}, **common,
            )  # fmt: skip
            raise
        self.append(
            action, phase="post", outcome="ok", pre_seq=pre.seq if pre else None,
            result_digest=eff.result_digest, details=eff.details, **common,
        )  # fmt: skip

    # ------------------------------------------------------------------ mirror

    def _mirror_last_seq(self) -> int:
        if self.mirror is None:
            return 0
        if not self.mirror.exists() or self.mirror.stat().st_size == 0:
            return 0
        with self.mirror.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell() - 1
            while pos > 0:
                fh.seek(pos - 1)
                if fh.read(1) == b"\n":
                    break
                pos -= 1
            fh.seek(max(pos, 0))
            line = fh.readline()
        try:
            return int(json.loads(line)["seq"])
        except (ValueError, KeyError):
            raise AuditError(f"{self.mirror}: last line is not a valid record") from None

    def _append_mirror(self, records: list[AuditRecord]) -> None:
        if self.mirror is None:
            return
        self.mirror.parent.mkdir(parents=True, exist_ok=True)
        with self.mirror.open("a", encoding="utf-8") as fh:
            for r in records:
                fh.write(r.to_json_line() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _sync_mirror_locked(self, conn: Connection) -> None:
        """Bring audit.jsonl up to date with audit.db (e.g. after a crash between the two)."""
        if self.mirror is None:
            return
        mirror_seq = self._mirror_last_seq()
        rows = conn.execute(
            select(audit_records)
            .where(audit_records.c.seq > mirror_seq)
            .order_by(audit_records.c.seq)
        ).all()
        if rows:
            self._append_mirror([_row_to_record(r) for r in rows])

    def sync_mirror(self) -> None:
        with self._write_lock() as conn:
            self._sync_mirror_locked(conn)

    # ------------------------------------------------------------------ reading

    def records(
        self,
        *,
        action: str | None = None,
        conversation_id: str | None = None,
        last: int | None = None,
    ) -> list[AuditRecord]:
        query = select(audit_records)
        if action:
            query = query.where(audit_records.c.action == action)
        if conversation_id:
            query = query.where(audit_records.c.conversation_id == conversation_id)
        if last:
            query = query.order_by(audit_records.c.seq.desc()).limit(last)
        with self._engine.connect() as conn:
            rows = conn.execute(query).all()
        return sorted((_row_to_record(r) for r in rows), key=lambda r: r.seq)

    def since(self, seq: int, limit: int = 500) -> list[AuditRecord]:
        """Records after `seq`, oldest first (for exporters that keep their place)."""
        query = (select(audit_records).where(audit_records.c.seq > seq)
                 .order_by(audit_records.c.seq).limit(limit))  # fmt: skip
        with self._engine.connect() as conn:
            return [_row_to_record(r) for r in conn.execute(query).all()]

    def latest(self, *actions: str) -> AuditRecord | None:
        query = select(audit_records).order_by(audit_records.c.seq.desc()).limit(1)
        if actions:
            query = query.where(audit_records.c.action.in_(actions))
        with self._engine.connect() as conn:
            row = conn.execute(query).first()
        return _row_to_record(row) if row else None

    def at(self, seq: int) -> AuditRecord | None:
        query = select(audit_records).where(audit_records.c.seq == seq)
        with self._engine.connect() as conn:
            row = conn.execute(query).first()
        return _row_to_record(row) if row else None

    def hashes(self, upto: int) -> list[str]:
        """Record hashes for seq 1..upto, in order: the leaves of the Merkle tree (merkle.py)."""
        query = (select(audit_records.c.hash).where(audit_records.c.seq <= upto)
                 .order_by(audit_records.c.seq))  # fmt: skip
        with self._engine.connect() as conn:
            return [str(h) for (h,) in conn.execute(query).all()]

    def head(self) -> tuple[int, str]:
        rec = self.latest()
        return (rec.seq, rec.hash) if rec else (0, GENESIS)

    # ------------------------------------------------------------------ verification

    def verify(self, expect: list[tuple[int, str]] | None = None) -> VerifyResult:
        """Recompute the chain, compare with the mirror, and check expected (seq, hash) pairs."""
        problems: list[str] = []
        notes: list[str] = []
        with self._engine.connect() as conn:
            rows = conn.execute(select(audit_records).order_by(audit_records.c.seq)).all()
        records: list[AuditRecord] = []
        prev, expected_seq, last_ts = GENESIS, 1, ""
        by_seq: dict[int, AuditRecord] = {}
        for row in rows:
            try:
                r = _row_to_record(row)
            except (ValueError, TypeError):
                problems.append(f"record {row.seq}: unreadable (details are not valid JSON)")
                prev, expected_seq = row.hash, row.seq + 1
                continue
            if row.details != canonical(r.details):
                problems.append(f"record {r.seq}: stored details were edited (not in written form)")
            records.append(r)
            by_seq[r.seq] = r
            if r.seq != expected_seq:
                problems.append(f"record {expected_seq}: missing (next present is {r.seq})")
                expected_seq = r.seq
            if r.prev_hash != prev:
                problems.append(f"record {r.seq}: does not link to the previous record")
            if r.compute_hash() != r.hash:
                problems.append(f"record {r.seq}: contents changed after it was written")
            if r.ts < last_ts:
                notes.append(f"record {r.seq}: timestamp earlier than the previous record")
            prev, last_ts, expected_seq = r.hash, r.ts, expected_seq + 1
            if len(problems) >= 20:
                problems.append("... stopping after 20 problems")
                break

        if self.mirror is None:
            notes.append("no mirror file: the audit database is encrypted (ADR-0019)")
        elif len(records) == len(rows):
            problems.extend(self._compare_mirror(records))
        for seq, h in expect or []:
            got = by_seq.get(seq)
            if got is None:
                problems.append(f"expected record {seq} is missing (log truncated or rewritten)")
            elif got.hash != h:
                problems.append(
                    f"record {seq}: hash differs from the expected head (log rewritten)"
                )
        head_seq, head_hash = (rows[-1].seq, rows[-1].hash) if rows else (0, GENESIS)
        return VerifyResult(not problems, len(rows), head_seq, head_hash, problems, notes)

    def _compare_mirror(self, records: list[AuditRecord]) -> list[str]:
        if self.mirror is None:
            return []
        if not self.mirror.exists():
            return [f"{self.mirror}: mirror file missing"] if records else []
        lines = self.mirror.read_text(encoding="utf-8").splitlines()
        out = []
        for i, r in enumerate(records):
            if i >= len(lines):
                out.append(f"mirror is missing records from {r.seq} on")
                break
            if lines[i] != r.to_json_line():
                out.append(f"record {r.seq}: database and mirror disagree (one was edited)")
                if len(out) >= 5:
                    break
        if len(lines) > len(records):
            out.append(f"mirror has {len(lines) - len(records)} record(s) the database lacks")
        return out
