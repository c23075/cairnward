"""Privacy settings, presets and resolution (spec §5.0, ADR-0012).

Every protection is a setting. A preset (open / balanced / strict) sets them all;
individual settings override the preset. Settings resolve in this order, first match
wins: environment setting > environment preset > global setting > global preset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator

PresetName = Literal["open", "balanced", "strict"]
OnOff = Literal["on", "off"]

Isolation = OnOff
Network = Literal["open", "ask", "allowlist", "off"]
HostedBackends = Literal["allow", "ask", "deny"]
TrainingBackends = Literal["allow", "scratch_only"]
Approvals = Literal["off", "high_only", "tiered"]
Redaction = Literal["off", "warn", "enforce"]
Vault = Literal["off", "digests", "full"]
Audit = OnOff
AuditAnchor = Literal["off", "git"]
CryptoShred = OnOff
BackupPurge = Literal["off", "ask", "on"]
McpPinning = OnOff
EgressReport = OnOff
HomeAccess = Literal["readable", "hidden"]

# The protection settings, in display order, with a one-line description each.
SETTINGS: dict[str, str] = {
    "isolation": "OS sandbox + network namespaces for agents, clients and the core",
    "network": "outbound access for tools and Cairnward",
    "home_access": "whether sandboxed coding agents can read the rest of your home folder",
    "hosted_backends": "use of hosted (cloud) model backends",
    "training_backends": "backends that may train on prompts (most free tiers): scratch only",
    "approvals": "risk-tiered approval prompts",
    "redaction": "scan outbound content Cairnward handles for secrets and personal data",
    "vault": "transcript vault (Cairnward's copy of conversations)",
    "audit": "hash-chained audit log",
    "audit_anchor": "external audit checkpoints",
    "crypto_shred": "per-conversation keys for reliable deletion",
    "backup_purge": "purging a conversation also rewrites backups",
    "mcp_pinning": "MCP tool pinning and change detection",
    "egress_report": "per-conversation egress report",
}


def _on_off(value: Any) -> Any:
    """Accept TOML booleans for on/off settings."""
    if value is True:
        return "on"
    if value is False:
        return "off"
    return value


class PrivacyOverrides(BaseModel):
    """A `[privacy]` table: an optional preset plus optional individual settings."""

    model_config = ConfigDict(extra="forbid")

    preset: PresetName | None = Field(default=None, description="open, balanced or strict")
    isolation: Isolation | None = None
    network: Network | None = None
    network_allow_hosts: list[str] | None = Field(
        default=None,
        description="hosts allowed when network = 'allowlist' or pre-approved in 'ask'",
    )
    home_access: HomeAccess | None = None
    hosted_backends: HostedBackends | None = None
    training_backends: TrainingBackends | None = None
    approvals: Approvals | None = None
    redaction: Redaction | None = None
    vault: Vault | None = None
    audit: Audit | None = None
    audit_anchor: AuditAnchor | None = None
    crypto_shred: CryptoShred | None = None
    backup_purge: BackupPurge | None = None
    mcp_pinning: McpPinning | None = None
    egress_report: EgressReport | None = None

    @field_validator(
        "isolation", "audit", "crypto_shred", "mcp_pinning", "egress_report", mode="before"
    )
    @classmethod
    def _bool_to_on_off(cls, value: Any) -> Any:
        return _on_off(value)


PRESETS: dict[str, dict[str, str]] = {
    "open": {
        "isolation": "off",
        "network": "open",
        "home_access": "readable",
        "hosted_backends": "allow",
        "training_backends": "allow",
        "approvals": "off",
        "redaction": "off",
        "vault": "off",
        "audit": "on",
        "audit_anchor": "off",
        "crypto_shred": "off",
        "backup_purge": "off",
        "mcp_pinning": "off",
        "egress_report": "off",
    },
    "balanced": {
        "isolation": "on",
        "network": "ask",
        "home_access": "hidden",
        "hosted_backends": "ask",
        "training_backends": "scratch_only",
        "approvals": "high_only",
        "redaction": "warn",
        "vault": "full",
        "audit": "on",
        "audit_anchor": "off",
        "crypto_shred": "on",
        "backup_purge": "ask",
        "mcp_pinning": "on",
        "egress_report": "on",
    },
    "strict": {
        "isolation": "on",
        "network": "allowlist",
        "home_access": "hidden",
        "hosted_backends": "deny",
        "training_backends": "scratch_only",
        "approvals": "tiered",
        "redaction": "enforce",
        "vault": "full",
        "audit": "on",
        "audit_anchor": "git",
        "crypto_shred": "on",
        "backup_purge": "on",
        "mcp_pinning": "on",
        "egress_report": "on",
    },
}

DEFAULT_PRESET: PresetName = "balanced"

# How protective each value is (higher = more protective), to report what is weaker than
# Strict. Vault "digests" stores less than "full", so it isn't weaker.
_STRENGTH: dict[str, dict[str, int]] = {
    "isolation": {"off": 0, "on": 1},
    "network": {"open": 0, "ask": 1, "allowlist": 2, "off": 3},
    "home_access": {"readable": 0, "hidden": 1},
    "hosted_backends": {"allow": 0, "ask": 1, "deny": 2},
    "training_backends": {"allow": 0, "scratch_only": 1},
    "approvals": {"off": 0, "high_only": 1, "tiered": 2},
    "redaction": {"off": 0, "warn": 1, "enforce": 2},
    "vault": {"off": 0, "digests": 2, "full": 2},
    "audit": {"off": 0, "on": 1},
    "audit_anchor": {"off": 0, "git": 1},
    "crypto_shred": {"off": 0, "on": 1},
    "backup_purge": {"off": 0, "ask": 1, "on": 2},
    "mcp_pinning": {"off": 0, "on": 1},
    "egress_report": {"off": 0, "on": 1},
}


_TYPES: dict[str, Any] = {
    "isolation": Isolation,
    "network": Network,
    "home_access": HomeAccess,
    "hosted_backends": HostedBackends,
    "training_backends": TrainingBackends,
    "approvals": Approvals,
    "redaction": Redaction,
    "vault": Vault,
    "audit": Audit,
    "audit_anchor": AuditAnchor,
    "crypto_shred": CryptoShred,
    "backup_purge": BackupPurge,
    "mcp_pinning": McpPinning,
    "egress_report": EgressReport,
}


def _check_tables() -> None:
    """Fail at import if presets, strengths and types ever drift apart."""
    if set(_TYPES) != set(SETTINGS) or set(_STRENGTH) != set(SETTINGS):
        raise RuntimeError("privacy tables out of sync")
    for name, literal in _TYPES.items():
        if set(get_args(literal)) != set(_STRENGTH[name]):
            raise RuntimeError(f"strength table incomplete for {name}")
    for preset, values in PRESETS.items():
        if set(values) != set(SETTINGS):
            raise RuntimeError(f"preset {preset} is missing settings")
        for name, value in values.items():
            if value not in get_args(_TYPES[name]):
                raise RuntimeError(f"preset {preset}: invalid {name} = {value}")


_check_tables()


@dataclass(frozen=True)
class ResolvedSetting:
    name: str
    value: str
    source: str  # e.g. "environment 'scratch'", "preset strict (global)"
    weaker_than_strict: bool


@dataclass(frozen=True)
class EffectivePrivacy:
    environment: str | None
    base_preset: PresetName
    settings: dict[str, ResolvedSetting]
    network_allow_hosts: list[str]
    warnings: list[str] = field(default_factory=list)  # settings that don't do what they say
    notes: list[str] = field(default_factory=list)  # general information

    def value(self, name: str) -> str:
        return self.settings[name].value

    def as_dict(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "label": self.label,
            "base_preset": self.base_preset,
            "custom": self.is_custom,
            "weaker_than_strict": self.weaker_than_strict,
            "settings": {
                n: {
                    "value": s.value,
                    "source": s.source,
                    "weaker_than_strict": s.weaker_than_strict,
                }
                for n, s in self.settings.items()
            },
            "changed_from_preset": self.changed_from_preset,
            "network_allow_hosts": self.network_allow_hosts,
            "warnings": self.warnings,
            "notes": self.notes,
        }

    @property
    def changed_from_preset(self) -> list[str]:
        return [n for n, s in self.settings.items() if s.value != PRESETS[self.base_preset][n]]

    @property
    def is_custom(self) -> bool:
        return bool(self.changed_from_preset)

    @property
    def weaker_than_strict(self) -> list[str]:
        return [n for n, s in self.settings.items() if s.weaker_than_strict]

    @property
    def label(self) -> str:
        """e.g. 'Strict', 'Balanced (custom: network)'."""
        name = self.base_preset.capitalize()
        changed = self.changed_from_preset
        if not changed:
            return name
        shown = ", ".join(changed[:3]) + (f" +{len(changed) - 3}" if len(changed) > 3 else "")
        return f"{name} (custom: {shown})"


def resolve_privacy(
    global_overrides: PrivacyOverrides,
    env_overrides: PrivacyOverrides | None = None,
    env_name: str | None = None,
) -> EffectivePrivacy:
    """Apply: environment setting > environment preset > global setting > global preset."""
    env = env_overrides or PrivacyOverrides()
    env_label = f"environment '{env_name}'" if env_name else "environment"
    global_preset: PresetName = global_overrides.preset or DEFAULT_PRESET
    global_preset_src = (
        f"preset {global_preset} (global)"
        if global_overrides.preset
        else f"default preset {global_preset}"
    )

    resolved: dict[str, ResolvedSetting] = {}
    for name in SETTINGS:
        env_value = getattr(env, name)
        global_value = getattr(global_overrides, name)
        if env_value is not None:
            value, source = env_value, env_label
        elif env.preset is not None:
            value, source = PRESETS[env.preset][name], f"preset {env.preset} ({env_label})"
        elif global_value is not None:
            value, source = global_value, "global setting"
        else:
            value, source = PRESETS[global_preset][name], global_preset_src
        weaker = _STRENGTH[name][value] < _STRENGTH[name][PRESETS["strict"][name]]
        resolved[name] = ResolvedSetting(name, value, source, weaker)

    base: PresetName = env.preset or global_preset
    hosts = env.network_allow_hosts
    if hosts is None:
        hosts = global_overrides.network_allow_hosts or []
    eff = EffectivePrivacy(env_name, base, resolved, list(hosts))
    eff.warnings.extend(_warnings(eff))
    eff.notes.extend(_notes(eff))
    return eff


def _warnings(eff: EffectivePrivacy) -> list[str]:
    v = eff.value
    out: list[str] = []
    if v("isolation") == "off" and v("network") != "open":
        out.append(
            f"network = '{v('network')}' is advisory only while isolation is off: "
            "tools can bypass the proxy and connect directly."
        )
    if v("audit") == "off" and v("audit_anchor") != "off":
        out.append("audit_anchor has no effect while audit is off.")
    if v("vault") == "off" and v("crypto_shred") == "on":
        out.append(
            "crypto_shred has no effect while the vault is off (no conversations are stored)."
        )
    if v("network") == "off" and v("hosted_backends") != "deny":
        out.append(
            f"hosted_backends = '{v('hosted_backends')}' has no effect while network is off."
        )
    if v("network") == "allowlist" and not eff.network_allow_hosts:
        out.append(
            "network = 'allowlist' with an empty network_allow_hosts list: nothing can go out."
        )
    out += [w for h in eff.network_allow_hosts if (w := allow_host_risk(h))]
    return out


METADATA_HOSTS = {"169.254.169.254", "fd00:ec2::254", "metadata.google.internal",
                  "metadata.azure.internal", "100.100.100.200"}  # fmt: skip


def allow_host_risk(entry: str) -> str | None:
    """Risky network_allow_hosts entries (OpenShell review): cloud metadata services (they
    hand out credentials), catch-alls, and this machine's or the local network's addresses
    (they reach services that never expected outside callers)."""
    import ipaddress

    host = entry.strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if host in METADATA_HOSTS:
        return (f"network_allow_hosts lists '{entry}', a cloud metadata service: tools could "
                "fetch this machine's cloud credentials")  # fmt: skip
    if host in ("*", "") or (host.startswith("*.") and "." not in host[2:]):
        return f"network_allow_hosts lists '{entry}', which allows (almost) everything"
    try:
        ip = ipaddress.ip_network(host, strict=False)
    except ValueError:
        if host in ("localhost",) or host.endswith((".local", ".lan", ".internal")):
            return (f"network_allow_hosts lists '{entry}': local services become reachable "
                    "by tools")  # fmt: skip
        return None
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return (f"network_allow_hosts lists '{entry}', an address on this machine or the "
                "local network: services there become reachable by tools")  # fmt: skip
    return None


def _notes(eff: EffectivePrivacy) -> list[str]:
    out: list[str] = []
    if eff.value("redaction") != "off":
        out.append(
            "redaction only covers traffic Cairnward handles itself (model, MCP and research "
            "calls), not third-party tools' own HTTPS connections."
        )
    if eff.value("audit_anchor") == "git":
        out.append("audit_anchor = 'git' needs a private repository configured (cairnward setup).")
    return out
