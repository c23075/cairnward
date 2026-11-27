"""I37, D85: TLS inspection with isolation off would let an agent read the local CA's key, so runs
in such an environment are refused unless the owner accepts it with allow_without_isolation,
which is then shown as a warning and recorded in the audit log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.audit.settings import diff, snapshot
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.egress import Sessions
from cairnward.egress.control import LocalEgress
from cairnward.gateway.clients import RunKeys
from cairnward.run import PROFILES, RunError, plan_run

RUN = {"run_id": "r1", "gateway_key": "k", "gateway_port": 8484, "egress_port": 8485,
       "egress_token": "t"}  # fmt: skip


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def config(paths: Paths, isolation: str, tls: str) -> Any:
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "environments" / "e.toml").write_text(
        f'[privacy]\nisolation = "{isolation}"\n\n[tls_inspection]\n{tls}\n'
    )
    return load_config(paths)


def plan(cfg: Any, tmp_path: Path) -> Any:
    return plan_run(cfg, environment="e", profile=PROFILES["aider"], model="cw/e:medium",
                    workspace=tmp_path / "ws", command=["aider"], run=RUN, srt=None,
                    settings_file=tmp_path / "srt.json", tmp_dir=tmp_path)  # fmt: skip


def test_inspection_without_isolation_is_refused(paths: Paths, tmp_path: Path) -> None:
    cfg = config(paths, "off", "enabled = true")
    why = cfg.tls_inspection_refused("e")
    assert why and "isolation is off" in why and "allow_without_isolation" in why
    with pytest.raises(RunError, match="isolation is off"):
        plan(cfg, tmp_path)
    assert any(
        w.startswith("runs in this environment are refused") for w in cfg.privacy("e").warnings
    )


@pytest.mark.parametrize(
    ("isolation", "tls"),
    [
        ("on", "enabled = true"),  # the CA key is outside every sandbox
        ("off", "enabled = false"),  # nothing to protect
        ("off", ""),  # the default: off
    ],
)
def test_other_combinations_are_not_refused(paths: Paths, isolation: str, tls: str) -> None:
    assert config(paths, isolation, tls).tls_inspection_refused("e") is None


def test_the_owner_can_accept_it_and_is_told(paths: Paths, tmp_path: Path) -> None:
    cfg = config(paths, "off", "enabled = true\nallow_without_isolation = true")
    assert cfg.tls_inspection_refused("e") is None
    assert any("allowed without isolation" in w and "I37" in w for w in cfg.privacy("e").warnings)
    assert "tls_inspection is on" in " ".join(plan(cfg, tmp_path).warnings)


async def test_the_core_refuses_the_run_and_records_it(paths: Paths) -> None:
    cfg = config(paths, "off", "enabled = true")
    sessions, run_keys = Sessions(), RunKeys()
    with AuditLog.open(paths) as audit:
        app = management_app(cfg, audit=audit, run_keys=run_keys,
                             egress=LocalEgress(sessions), isolated=False)  # fmt: skip
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cairnward") as c:  # fmt: skip
            body = {"environment": "e", "client": "aider", "profile": "aider", "workspace": "/w",
                    "program": "aider", "arg_count": 0, "sandbox": "none",
                    "tls_trust": True}  # fmt: skip
            resp = await c.post("/v1/runs", json=body)
        assert resp.status_code == 403 and "isolation is off" in resp.text
        assert run_keys.environment == {}  # no key was issued
        last = audit.records()[-1]
        assert (last.action, last.environment, last.client) == ("run.refused", "e", "aider")


def test_turning_it_on_or_allowing_it_is_audited(paths: Paths) -> None:
    before = snapshot(config(paths, "off", "enabled = false"))
    on = snapshot(config(paths, "on", "enabled = true"))
    allowed = snapshot(config(paths, "off", "enabled = true\nallow_without_isolation = true"))
    assert {"scope": "environment:e", "setting": "tls_inspection", "before": None,
            "after": "on"} in diff(before, on)  # fmt: skip
    assert {"scope": "environment:e", "setting": "tls_inspection", "before": "on",
            "after": "on, allowed without isolation"} in diff(on, allowed)  # fmt: skip
    # environments without inspection keep their earlier snapshots: no change recorded
    assert "tls_inspection" not in before["environments"][0]
