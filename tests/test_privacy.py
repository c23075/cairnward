import pytest
from pydantic import ValidationError

from cairnward.config.privacy import (
    PRESETS,
    SETTINGS,
    PrivacyOverrides,
    resolve_privacy,
)


def test_default_is_balanced_and_not_custom() -> None:
    eff = resolve_privacy(PrivacyOverrides())
    assert eff.base_preset == "balanced"
    assert eff.label == "Balanced"
    assert all(eff.value(n) == PRESETS["balanced"][n] for n in SETTINGS)
    assert eff.settings["isolation"].source == "default preset balanced"


@pytest.mark.parametrize("preset", ["open", "balanced", "strict"])
def test_presets_apply_every_setting(preset: str) -> None:
    eff = resolve_privacy(PrivacyOverrides(preset=preset))  # type: ignore[arg-type]
    assert {n: eff.value(n) for n in SETTINGS} == PRESETS[preset]
    assert eff.label == preset.capitalize()


def test_strict_has_nothing_weaker_than_strict() -> None:
    assert resolve_privacy(PrivacyOverrides(preset="strict")).weaker_than_strict == []


def test_resolution_order() -> None:
    """environment setting > environment preset > global setting > global preset"""
    glob = PrivacyOverrides(preset="strict", approvals="high_only", redaction="warn")
    env = PrivacyOverrides(preset="open", redaction="enforce")
    eff = resolve_privacy(glob, env, "x")
    assert eff.value("redaction") == "enforce"  # environment setting
    assert eff.settings["redaction"].source == "environment 'x'"
    assert eff.value("approvals") == "off"  # environment preset beats global setting
    assert eff.settings["approvals"].source == "preset open (environment 'x')"
    eff2 = resolve_privacy(glob, PrivacyOverrides(), "y")
    assert eff2.value("approvals") == "high_only"  # global setting
    assert eff2.settings["approvals"].source == "global setting"
    assert eff2.value("network") == "allowlist"  # global preset
    assert eff2.settings["network"].source == "preset strict (global)"


def test_custom_label_lists_changes() -> None:
    eff = resolve_privacy(PrivacyOverrides(preset="strict", isolation="off"))
    assert eff.is_custom
    assert eff.changed_from_preset == ["isolation"]
    assert eff.label == "Strict (custom: isolation)"
    assert eff.weaker_than_strict == ["isolation"]


def test_vault_digests_is_not_weaker() -> None:
    eff = resolve_privacy(PrivacyOverrides(preset="strict", vault="digests"))
    assert eff.weaker_than_strict == []
    assert eff.is_custom


def test_booleans_accepted_for_on_off() -> None:
    o = PrivacyOverrides.model_validate({"isolation": False, "audit": True})
    assert (o.isolation, o.audit) == ("off", "on")


def test_unknown_and_invalid_values_rejected() -> None:
    with pytest.raises(ValidationError):
        PrivacyOverrides.model_validate({"netwrok": "off"})
    with pytest.raises(ValidationError):
        PrivacyOverrides.model_validate({"network": "sometimes"})


def test_warnings_for_ineffective_combinations() -> None:
    eff = resolve_privacy(PrivacyOverrides(preset="strict", isolation="off", audit="off"))
    text = " ".join(eff.warnings)
    assert "advisory only" in text
    assert "audit_anchor has no effect" in text
    assert "empty network_allow_hosts" in text


def test_everything_off_is_possible() -> None:
    every = {
        "isolation": "off",
        "network": "open",
        "hosted_backends": "allow",
        "approvals": "off",
        "redaction": "off",
        "vault": "off",
        "audit": "off",
        "audit_anchor": "off",
        "crypto_shred": "off",
        "backup_purge": "off",
        "mcp_pinning": "off",
        "egress_report": "off",
    }
    eff = resolve_privacy(PrivacyOverrides.model_validate(every))
    assert len(eff.weaker_than_strict) == len(SETTINGS)
    assert eff.warnings == []


def test_allow_hosts_environment_replaces_global() -> None:
    glob = PrivacyOverrides(network_allow_hosts=["a.example"])
    assert resolve_privacy(glob).network_allow_hosts == ["a.example"]
    env = PrivacyOverrides(network_allow_hosts=["b.example"])
    assert resolve_privacy(glob, env, "e").network_allow_hosts == ["b.example"]
