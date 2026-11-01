"""Record privacy-settings changes in the audit log (spec §5.0 D58, FR-PRIV-5).

Settings live in files the user edits, so Cairnward records changes when it next loads
them (at `cairnward config check` now; at every `cairnward serve` start later): it
compares the effective settings with the last `settings.snapshot` record.

Switching `audit` off globally writes `audit.disabled` and then nothing else is recorded
(except these markers). Switching it back on writes `audit.enabled` with the length of
the gap, so gaps are visible in the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cairnward.audit.log import AuditLog
from cairnward.config.loader import Config
from cairnward.config.privacy import SETTINGS


def snapshot(cfg: Config) -> dict[str, Any]:
    """Effective privacy settings globally and per environment (values only, no secrets)."""

    def one(env: str | None) -> dict[str, Any]:
        eff = cfg.privacy(env)
        out: dict[str, Any] = {n: eff.value(n) for n in SETTINGS}
        out["label"] = eff.label
        out["network_allow_hosts"] = sorted(eff.network_allow_hosts)
        return out

    # A list, not a dict keyed by name: environment names must never be read as detail keys.
    return {
        "global": one(None),
        "environments": [{"name": name, **one(name)} for name in sorted(cfg.environments)],
    }


def _envs(snap: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        e["name"]: {k: v for k, v in e.items() if k != "name"} for e in snap.get("environments", [])
    }


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    scopes = [("global", before.get("global", {}), after["global"])]
    b_envs, a_envs = _envs(before), _envs(after)
    for name in sorted(set(b_envs) | set(a_envs)):
        scopes.append((f"environment:{name}", b_envs.get(name, {}), a_envs.get(name, {})))
    for scope, b, a in scopes:
        if not b and a:
            changes.append({"scope": scope, "change": "added"})
            continue
        if b and not a:
            changes.append({"scope": scope, "change": "removed"})
            continue
        for key in sorted(set(b) | set(a)):
            if b.get(key) != a.get(key):
                changes.append(
                    {"scope": scope, "setting": key, "before": b.get(key), "after": a.get(key)}
                )
    return changes


@dataclass
class SettingsSync:
    changes: list[dict[str, Any]]
    disabled: bool = False
    enabled_after_gap_s: float | None = None
    first_snapshot: bool = False


def record_settings(cfg: Config, log: AuditLog, *, actor: str = "user") -> SettingsSync:
    """Compare effective settings with the last snapshot and record any change."""
    current = snapshot(cfg)
    audit_on = current["global"]["audit"] == "on"
    previous = log.latest("settings.snapshot")
    marker = log.latest("audit.disabled", "audit.enabled")
    currently_disabled = marker is not None and marker.action == "audit.disabled"

    if previous is None:
        log.append("settings.snapshot", actor=actor, details={"initial": True, **current})
        result = SettingsSync([], first_snapshot=True)
        if not audit_on:
            log.append("audit.disabled", actor=actor, details={"reason": "setting audit = off"})
            result.disabled = True
        return result

    changes = diff(previous.details, current)
    result = SettingsSync(changes)
    if audit_on and currently_disabled and marker is not None:
        gap = datetime.now(datetime.fromisoformat(marker.ts).tzinfo) - datetime.fromisoformat(
            marker.ts
        )
        result.enabled_after_gap_s = round(gap.total_seconds(), 3)
        log.append(
            "audit.enabled",
            actor=actor,
            details={"gap_seconds": result.enabled_after_gap_s, "disabled_at_seq": marker.seq},
        )
    if changes:
        log.append("settings.changed", actor=actor, details={"changes": changes})
        log.append("settings.snapshot", actor=actor, details=current)
    if not audit_on and not currently_disabled:
        log.append("audit.disabled", actor=actor, details={"reason": "setting audit = off"})
        result.disabled = True
    return result


def enabled_for(cfg: Config) -> Any:
    """`enabled_for` callback for AuditLog: is auditing on in this environment?"""

    def check(environment: str | None) -> bool:
        env = environment if environment in cfg.environments else None
        return cfg.privacy(env).value("audit") == "on"

    return check
