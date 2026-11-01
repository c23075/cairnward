"""Runs as recorded in the audit log, with their snapshots (`cairnward runs`)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cairnward.audit.log import AuditLog
from cairnward.run.snapshots import Snapshots


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    started: str
    environment: str | None
    client: str | None
    program: str | None
    workspace: Path | None
    exit_code: int | None
    duration_s: float | None
    changed_files: int | None


def list_runs(audit: AuditLog) -> list[RunInfo]:
    ends = {r.run_id: r for r in audit.records(action="run.end") if r.run_id}
    out = []
    for r in audit.records(action="run.start"):
        if not r.run_id:
            continue
        end = ends.get(r.run_id)
        ws = r.details.get("workspace")
        out.append(RunInfo(
            run_id=r.run_id, started=r.ts, environment=r.environment, client=r.client,
            program=r.target, workspace=Path(ws) if ws else None,
            exit_code=end.details.get("exit_code") if end else None,
            duration_s=end.details.get("duration_s") if end else None,
            changed_files=end.details.get("changed_files") if end else None,
        ))  # fmt: skip
    return out


class RunNotFound(Exception):
    pass


def find_run(audit: AuditLog, prefix: str) -> RunInfo:
    matches = [r for r in list_runs(audit) if r.run_id.startswith(prefix)]
    if not matches:
        raise RunNotFound(f"no run starting with '{prefix}'")
    if len(matches) > 1:
        raise RunNotFound(f"'{prefix}' matches {len(matches)} runs; give more characters")
    return matches[0]


def snapshots_for(state_dir: Path, run: RunInfo) -> Snapshots:
    if run.workspace is None:
        raise RunNotFound(f"run {run.run_id[:8]} has no workspace recorded")
    snaps = Snapshots(state_dir, run.workspace)
    if not snaps.has_run(run.run_id):
        raise RunNotFound(
            f"run {run.run_id[:8]} has no snapshots (taken before snapshots existed, or pruned)"
        )
    return snaps
