import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.gateway import ClientKeys, GatewayDeps, create_app
from cairnward.gateway.scheduling import BenchProfile, Budget, RunUsage, Scheduler
from cairnward.policy import PolicyEngine
from tests.fakes.backends import FakeOllama

# ------------------------------------------------------------------ queue and affinity


async def test_fifo_queue_with_one_slot() -> None:
    s = Scheduler()
    order: list[str] = []

    async def job(name: str, key: str, hold: float) -> None:
        async with s.slot("b", key):
            order.append(name)
            await asyncio.sleep(hold)

    first = asyncio.create_task(job("a", "k1", 0.05))
    await asyncio.sleep(0.01)
    rest = [asyncio.create_task(job(n, k, 0)) for n, k in (("b", "k2"), ("c", "k3"))]
    await asyncio.gather(first, *rest)
    assert order == ["a", "b", "c"]


async def test_warm_conversation_goes_first_but_cannot_starve_others() -> None:
    s = Scheduler()
    order: list[str] = []

    async def job(name: str, key: str) -> None:
        async with s.slot("b", key):
            order.append(name)
            await asyncio.sleep(0.01)

    first = asyncio.create_task(job("warm-1", "warm"))
    await asyncio.sleep(0.002)
    tasks = [asyncio.create_task(job("cold-1", "cold"))]
    await asyncio.sleep(0.002)
    for i in range(2, 6):
        tasks.append(asyncio.create_task(job(f"warm-{i}", "warm")))
        await asyncio.sleep(0.001)
    await asyncio.gather(first, *tasks)
    # warm requests jump ahead (cache is warm), but only 3 in a row; then the cold one runs
    assert order.index("cold-1") == 3
    assert order[:3] == ["warm-1", "warm-2", "warm-3"]


async def test_cancelled_waiter_does_not_leak_the_slot() -> None:
    s = Scheduler()
    hold = asyncio.Event()

    async def holder() -> None:
        async with s.slot("b", "k"):
            await hold.wait()

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    w = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    w.cancel()
    hold.set()
    await h
    with pytest.raises(asyncio.CancelledError):
        await w
    async with s.slot("b", "k") as waited:  # free again
        assert waited < 0.05


async def test_parallel_slots() -> None:
    s = Scheduler({"gpu": 2})
    inside = 0
    peak = 0

    async def job() -> None:
        nonlocal inside, peak
        async with s.slot("gpu", "k"):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(job() for _ in range(5)))
    assert peak == 2


def test_cached_prefix_estimate() -> None:
    s = Scheduler()
    history = [{"role": "system", "content": "x" * 400}, {"role": "user", "content": "y" * 400}]
    cold = s.estimate("b", history)
    assert cold.cached_tokens == 0 and cold.new_tokens == cold.prompt_tokens
    s.served("b", [*history, {"role": "assistant", "content": "ok"}])
    nxt = [*history, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "z" * 40}]
    warm = s.estimate("b", nxt)
    assert warm.cached_tokens > 200 and warm.new_tokens < 30


# ------------------------------------------------------------------ profile and budgets


def test_bench_profile_import(tmp_path: Path) -> None:
    result = tmp_path / "bench.json"
    result.write_text(
        json.dumps(
            {"model": "qwen3:30b", "summary": {"prompt_tps_median": 20.0, "gen_tps_median": 6.7}}
        )
    )
    profile = BenchProfile.at(tmp_path)
    assert profile.import_results([result]) == ["qwen3:30b"]
    again = BenchProfile.at(tmp_path)
    assert again.expected_seconds("qwen3:30b", 1200) == 60.0
    assert again.expected_seconds("other", 1200) is None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": "m", "summary": {}}))
    with pytest.raises(ValueError):
        profile.import_results([bad])


def test_run_usage() -> None:
    u = RunUsage(Budget(max_requests=2, max_tokens=100))
    assert u.exceeded() is None
    u.requests = 2
    assert "2 requests" in (u.exceeded() or "")
    u2 = RunUsage(Budget(max_minutes=0.0001))
    import time

    time.sleep(0.01)
    assert "minutes" in (u2.exceeded() or "")


# ------------------------------------------------------------------ in the gateway


async def make(
    tmp_path: Path, env: str = "", profile: dict[str, Any] | None = None, handler: Any = None
) -> tuple[httpx.AsyncClient, GatewayDeps, AuditLog, FakeOllama]:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "e.toml").write_text(env)
    cfg = load_config(paths)
    fake = FakeOllama()
    backends = Backends(
        cfg.backends, transport_for=lambda n: httpx.MockTransport(handler or fake.handler)
    )
    audit = AuditLog.open(paths)
    bp = BenchProfile.at(paths.state_dir)
    for model, tps in (profile or {}).items():
        from cairnward.gateway.scheduling import Speed

        bp.speeds[model] = Speed(tps, None)
    deps = GatewayDeps(
        cfg, PolicyEngine(cfg, audit=audit), backends, ClientKeys.at(paths.state_dir), profile=bp
    )
    app = create_app(deps)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8484"
    )
    return client, deps, audit, fake


async def test_run_budget_enforced(tmp_path: Path) -> None:
    client, deps, audit, fake = await make(tmp_path)
    key = deps.run_keys.issue("aider", "run-1", Budget(max_requests=2))
    h = {"Authorization": f"Bearer {key}"}
    body = {"model": "cw/e:low", "messages": [{"role": "user", "content": "hi"}]}
    for _ in range(2):
        assert (await client.post("/v1/chat/completions", headers=h, json=body)).status_code == 200
    resp = await client.post("/v1/chat/completions", headers=h, json=body)
    assert resp.status_code == 429 and resp.json()["error"]["type"] == "budget_exceeded"
    assert len(fake.requests) == 2
    assert audit.records()[-1].action == "budget.exceeded"
    assert deps.run_keys.usage["run-1"].tokens == 34  # 2 x (12 + 5)
    await client.aclose()
    audit.close()


async def test_context_overflow_warns_or_rejects(tmp_path: Path) -> None:
    big = [{"role": "user", "content": "word " * 40000}]  # ~50K tokens > 8K context
    client, deps, audit, _ = await make(tmp_path)
    key = ClientKeys.at(deps.config.paths.state_dir).add("w")
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "cw/e:low", "messages": big},
    )
    assert resp.status_code == 200
    assert "context is 8192" in resp.headers["x-cairnward-warning"]
    pre = next(r for r in audit.records() if r.phase == "pre")
    assert pre.details["warnings"] and pre.details["est_prompt_tokens"] > 8192
    await client.aclose()
    audit.close()

    client, deps, audit, fake = await make(tmp_path / "2", env='context_overflow = "reject"\n')
    key = ClientKeys.at(deps.config.paths.state_dir).add("w")
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "cw/e:low", "messages": big},
    )
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "context_length_exceeded"
    assert fake.requests == []
    await client.aclose()
    audit.close()


async def test_slow_request_warning_uses_profile_and_cache(tmp_path: Path) -> None:
    client, deps, audit, _ = await make(tmp_path, profile={"qwen3:1.7b": 10.0})
    key = ClientKeys.at(deps.config.paths.state_dir).add("w")
    h = {"Authorization": f"Bearer {key}"}
    first = [{"role": "user", "content": "x " * 1500}]  # ~750 tokens at 10 tok/s = 75 s
    r1 = await client.post(
        "/v1/chat/completions", headers=h, json={"model": "cw/e:low", "messages": first}
    )
    assert "min before the first word" in r1.headers.get("x-cairnward-warning", "")
    follow = [*first, r1.json()["choices"][0]["message"], {"role": "user", "content": "and?"}]
    r2 = await client.post(
        "/v1/chat/completions", headers=h, json={"model": "cw/e:low", "messages": follow}
    )
    assert "x-cairnward-warning" not in r2.headers  # the long start is cached: fast
    pres = [r for r in audit.records() if r.phase == "pre"]
    assert pres[1].details["est_new_tokens"] < 50
    await client.aclose()
    audit.close()


async def test_concurrent_requests_queue(tmp_path: Path) -> None:
    fake = FakeOllama()

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return fake.handler(request)

    client, deps, audit, _ = await make(tmp_path, handler=slow)
    key = ClientKeys.at(deps.config.paths.state_dir).add("w")
    h = {"Authorization": f"Bearer {key}"}

    def req(text: str) -> Any:
        return client.post(
            "/v1/chat/completions",
            headers=h,
            json={"model": "cw/e:low", "messages": [{"role": "user", "content": text}]},
        )

    r = await asyncio.gather(req("a"), req("b"))
    assert all(x.status_code == 200 for x in r)
    posts = [x for x in audit.records() if x.phase == "post"]
    assert max(p.details["queued_s"] for p in posts) >= 0.08  # one waited for the other
    await client.aclose()
    audit.close()
