"""Agent profiles' first-prompt sizes, measured per run and flagged (FR-AGT-1)."""

import json
from pathlib import Path

import httpx

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.gateway.clients import RunKeys
from cairnward.gateway.scheduling import PromptSizes
from cairnward.run.runner import prompt_warnings


async def test_recorded_at_run_end(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    with AuditLog.open(paths) as audit:
        app = management_app(cfg, audit=audit, run_keys=RunKeys(), egress=None, isolated=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cairnward") as c:  # fmt: skip
            body = {
                "environment": "coding",
                "client": "qwen-code",
                "profile": "qwen-code",
                "workspace": "/w",
                "program": "qwen",
                "arg_count": 0,
                "sandbox": "none",
            }
            run = (await c.post("/v1/runs", json=body)).json()
            for tokens in (14_200, 15_000):  # the first call counts
                audit.append("model.call", phase="post", target="local-ollama",
                             run_id=run["run_id"], details={"prompt_tokens": tokens})  # fmt: skip
            await c.post(f"/v1/runs/{run['run_id']}/end", json={"exit_code": 0, "duration_s": 1})
    sizes = PromptSizes.at(paths.state_dir)
    assert sizes.get("qwen-code") == 14_200 and sizes.get("aider") is None
    sizes.note("qwen-code", 9_000)
    assert sizes.all()["qwen-code"] == {"last": 9_000, "max": 14_200, "runs": 2}


def test_warnings(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    assert prompt_warnings(cfg, "qwen-code", "coding", "cw/coding:medium") == []  # nothing measured
    PromptSizes.at(paths.state_dir).note("qwen-code", 40_000)
    warns = prompt_warnings(cfg, "qwen-code", "coding", "cw/coding:medium")
    assert len(warns) == 1 and "more than the" in warns[0]
    (paths.state_dir / "bench_profile.json").write_text(json.dumps(
        {"qwen3:30b": {"prompt_tps": 20.0, "gen_tps": 8.0}}))  # fmt: skip
    PromptSizes.at(paths.state_dir).note("qwen-code", 6_000)
    warns = prompt_warnings(cfg, "qwen-code", "coding", "cw/coding:medium")
    assert len(warns) == 1 and "about 5 min before the first answer" in warns[0]
