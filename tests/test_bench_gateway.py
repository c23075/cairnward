"""The gateway overhead benchmark runs, on real sockets (NFR-P2). This guards against large
regressions only: the 75 ms and 10 ms targets depend on the disk and CPU, and shared CI
machines vary, so they're checked by running the script on the reference machine
(docs/bench/results-2026-11-03-gateway-overhead.md)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "bench_gateway.py"
CEILING_MS = 500.0


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_gateway", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("mode", ["strict", "all-off"])
def test_overhead_has_not_grown_wildly(mode: str, tmp_path: Path) -> None:
    result = load().measure(mode, requests=20, folder=tmp_path)
    assert result["mode"] == mode and result["direct_median_ms"] > 0
    assert result["overhead_median_ms"] < CEILING_MS


def test_all_off_really_turns_the_records_off(tmp_path: Path) -> None:
    """Otherwise the all-off numbers would measure the wrong thing. The real server opens
    the audit log with enabled_for(cfg); the benchmark must too."""
    bench = load()
    with bench.stand_in() as url, bench.gateway(url, bench.MODES["all-off"][0], tmp_path) as (
        gw, key,
    ):  # fmt: skip
        import httpx

        h = {"Authorization": f"Bearer {key}"}
        body = {"model": "cw/coding:low", "messages": bench.MESSAGES}
        assert httpx.post(f"{gw}/v1/chat/completions", json=body, headers=h,
                          trust_env=False, timeout=30).status_code == 200  # fmt: skip
    mirror = tmp_path / "data" / "audit.jsonl"
    assert '"model.call"' not in (mirror.read_text() if mirror.exists() else "")
