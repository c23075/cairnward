import asyncio
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.models import Client, ClientBudget
from cairnward.config.paths import Paths
from cairnward.gateway import ClientKeys, GatewayDeps, create_app
from cairnward.gateway.scheduling import BenchProfile, Budget, ClientUsage, RunUsage, Scheduler
from cairnward.policy import PolicyEngine
from tests.fakes.backends import FakeOllama

# ------------------------------------------------------------------ queue and affinity


async def _queued(s: Scheduler, backend: str, n: int) -> None:
    """Wait until n requests are waiting (deterministic: no reliance on timer resolution)."""
    for _ in range(1000):
        if s.queued(backend) >= n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {n} waiting requests, have {s.queued(backend)}")


async def test_fifo_queue_with_one_slot() -> None:
    s = Scheduler()
    order: list[str] = []
    release = asyncio.Event()

    async def job(name: str, key: str, gate: asyncio.Event | None = None) -> None:
        async with s.slot("b", key):
            order.append(name)
            if gate:
                await gate.wait()

    first = asyncio.create_task(job("a", "k1", release))
    await asyncio.sleep(0)
    rest = []
    for n, (name, key) in enumerate((("b", "k2"), ("c", "k3")), start=1):
        rest.append(asyncio.create_task(job(name, key)))
        await _queued(s, "b", n)
    release.set()
    await asyncio.gather(first, *rest)
    assert order == ["a", "b", "c"]


async def test_warm_conversation_goes_first_but_cannot_starve_others() -> None:
    s = Scheduler()
    order: list[str] = []
    release = asyncio.Event()

    async def job(name: str, key: str, gate: asyncio.Event | None = None) -> None:
        async with s.slot("b", key):
            order.append(name)
            if gate:
                await gate.wait()

    first = asyncio.create_task(job("warm-1", "warm", release))
    await asyncio.sleep(0)
    tasks = [asyncio.create_task(job("cold-1", "cold"))]
    await _queued(s, "b", 1)
    for i in range(2, 6):
        tasks.append(asyncio.create_task(job(f"warm-{i}", "warm")))
        await _queued(s, "b", i)
    release.set()
    await asyncio.gather(first, *tasks)
    # warm requests jump ahead (cache is warm), but only 3 in a row; then the cold one runs
    assert order[:4] == ["warm-1", "warm-2", "warm-3", "cold-1"]


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
    u2 = RunUsage(Budget(max_minutes=1))
    assert u2.exceeded() is None
    u2.started -= 61  # no sleeping: Windows' clock can read the same across a short sleep
    assert "1 minutes elapsed" in (u2.exceeded() or "")


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


CHAT = {"model": "cw/e:low", "messages": [{"role": "user", "content": "hi"}]}


def limited(deps: GatewayDeps, name: str, **limits: int) -> None:
    deps.config.clients.clients[name] = Client(kind="agent", profile="aider",
                                               budget=ClientBudget(**limits))  # fmt: skip


async def test_run_is_pinned_to_its_environment(tmp_path: Path) -> None:
    client, deps, audit, fake = await make(tmp_path)
    deps.config.environments["other"] = deps.config.environments["e"]
    deps.config.clients.clients["aider"] = Client(kind="agent", profile="aider",
                                                  default_environment="other")  # fmt: skip
    h = {"Authorization": f"Bearer {deps.run_keys.issue('aider', 'run-1', environment='e')}"}
    chat = "/v1/chat/completions"
    assert (await client.post(chat, headers=h, json=CHAT)).status_code == 200
    calls = len(fake.requests)

    # another environment is refused and audited, and never reaches the model
    for path, model, body in (
        (chat, "cw/other:low", CHAT),
        ("/v1/embeddings", "cw/other:embed", {"input": "hi"}),
    ):
        resp = await client.post(path, headers=h, json={**body, "model": model})
        assert resp.status_code == 403 and resp.json()["error"]["type"] == "run_environment"
        assert "this run is in 'e'" in resp.json()["error"]["message"]
        last = audit.records()[-1]
        assert (last.action, last.decision, last.environment) == ("model.call", "deny", "e")
        assert last.run_id == "run-1"
        assert last.details["requested_environment"] == "other"
        assert last.details["virtual_model"] == model
    assert len(fake.requests) == calls

    # cw/raw/<model> uses the run's environment, not the client's default
    for path, body in ((chat, CHAT), ("/v1/embeddings", {"input": "hi"})):
        resp = await client.post(path, headers=h, json={**body, "model": "cw/raw/m"})
        assert resp.status_code == 200
        assert audit.records()[-1].environment == "e"
    own = {"model": "cw/e:embed", "input": "hi"}
    assert (await client.post("/v1/embeddings", headers=h, json=own)).status_code == 200

    # the client's own key isn't tied to one environment
    plain = {"Authorization": f"Bearer {ClientKeys.at(deps.config.paths.state_dir).add('aider')}"}
    resp = await client.post(chat, headers=plain, json={**CHAT, "model": "cw/other:low"})
    assert resp.status_code == 200 and audit.records()[-1].environment == "other"
    await client.aclose()
    audit.close()


async def test_disabled_client_is_refused(tmp_path: Path) -> None:
    client, deps, audit, fake = await make(tmp_path)
    limited(deps, "w")
    plain = {"Authorization": f"Bearer {ClientKeys.at(deps.config.paths.state_dir).add('w')}"}
    run = {"Authorization": f"Bearer {deps.run_keys.issue('w', 'run-1', environment='e')}"}
    # initialize is answered by the MCP SDK alone, so the hub itself isn't needed here
    mcp_deps = dataclasses.replace(deps, mcp=SimpleNamespace(cfg=deps.config))
    mcp_app = create_app(mcp_deps)
    mcp = httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_app),
                            base_url="http://127.0.0.1:8484")  # fmt: skip
    hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"}}}  # fmt: skip
    accept = {"Accept": "application/json, text/event-stream"}

    async def statuses(h: dict[str, str]) -> list[int]:
        return [
            (await client.post("/v1/chat/completions", headers=h, json=CHAT)).status_code,
            (await client.post("/v1/embeddings", headers=h,
                               json={"model": "cw/e:embed", "input": "hi"})).status_code,
            (await client.get("/v1/models", headers=h)).status_code,
            (await mcp.post("/mcp", headers={**h, **accept}, json=hello)).status_code,
        ]  # fmt: skip

    assert await statuses(plain) == [200, 200, 200, 200]
    assert await statuses(run) == [200, 200, 200, 200]
    calls = len(fake.requests)
    deps.config.clients.clients["w"] = Client(kind="agent", profile="aider", enabled=False)
    assert await statuses(plain) == [401, 401, 401, 401]
    assert await statuses(run) == [401, 401, 401, 401]
    assert len(fake.requests) == calls
    # switching it back on restores the same keys
    limited(deps, "w")
    assert await statuses(run) == [200, 200, 200, 200]
    await mcp_app.state.mcp_manager.aclose()
    await mcp.aclose()
    await client.aclose()
    audit.close()


async def test_client_daily_request_budget(tmp_path: Path) -> None:
    client, deps, audit, fake = await make(tmp_path)
    limited(deps, "w", max_requests_per_day=2)
    h = {"Authorization": f"Bearer {ClientKeys.at(deps.config.paths.state_dir).add('w')}"}
    for _ in range(2):
        assert (await client.post("/v1/chat/completions", headers=h, json=CHAT)).status_code == 200
    resp = await client.post("/v1/chat/completions", headers=h, json=CHAT)
    assert resp.status_code == 429 and resp.json()["error"]["type"] == "budget_exceeded"
    assert "clients.toml" in resp.json()["error"]["message"]
    assert len(fake.requests) == 2  # the third never reached the model
    last = audit.records()[-1]
    assert last.action == "budget.exceeded" and last.client == "w"
    await client.aclose()
    audit.close()


async def test_client_token_budget_counts_its_runs_and_streams(tmp_path: Path) -> None:
    client, deps, audit, fake = await make(tmp_path)
    limited(deps, "aider", max_tokens_per_day=30)
    key = deps.run_keys.issue("aider", "run-1")
    h = {"Authorization": f"Bearer {key}"}
    assert (await client.post("/v1/chat/completions", headers=h, json=CHAT)).status_code == 200
    stream = await client.post("/v1/chat/completions", headers=h, json={**CHAT, "stream": True})
    assert stream.status_code == 200
    assert ClientUsage.at(deps.config.paths.state_dir).get("aider") == (2, 34)
    # a new run of the same client doesn't start again from zero
    other = {"Authorization": f"Bearer {deps.run_keys.issue('aider', 'run-2')}"}
    resp = await client.post("/v1/chat/completions", headers=other, json=CHAT)
    assert resp.status_code == 429 and "30 tokens used today" in resp.json()["error"]["message"]
    assert len(fake.requests) == 2
    await client.aclose()
    audit.close()


async def test_client_budget_covers_embeddings(tmp_path: Path) -> None:
    client, deps, audit, _ = await make(tmp_path)
    limited(deps, "w", max_requests_per_day=1)
    h = {"Authorization": f"Bearer {ClientKeys.at(deps.config.paths.state_dir).add('w')}"}
    body = {"model": "cw/e:embed", "input": "hi"}
    assert (await client.post("/v1/embeddings", headers=h, json=body)).status_code == 200
    assert (await client.post("/v1/embeddings", headers=h, json=body)).status_code == 429
    await client.aclose()
    audit.close()


async def test_client_without_a_limit_is_not_tracked(tmp_path: Path) -> None:
    client, deps, audit, _ = await make(tmp_path)
    limited(deps, "w")
    h = {"Authorization": f"Bearer {ClientKeys.at(deps.config.paths.state_dir).add('w')}"}
    assert (await client.post("/v1/chat/completions", headers=h, json=CHAT)).status_code == 200
    assert not (deps.config.paths.state_dir / "client_usage.json").exists()
    await client.aclose()
    audit.close()


def test_client_usage_survives_restart_and_resets_next_day(tmp_path: Path) -> None:
    day = ["2026-11-03"]
    path = tmp_path / "client_usage.json"
    usage = ClientUsage(path, today=lambda: day[0])
    limits = ClientBudget(max_requests_per_day=2)
    usage.add("w", 10)
    assert usage.exceeded("w", limits) is None
    usage.add("w", 5)
    again = ClientUsage(path, today=lambda: day[0])  # the server restarted
    assert again.get("w") == (2, 15) and "2 requests used today" in str(again.exceeded("w", limits))
    assert "w" in json.loads(path.read_text()) and "hi" not in path.read_text()
    day[0] = "2026-11-04"
    assert again.get("w") == (0, 0) and again.exceeded("w", limits) is None
    again.add("w", 1)
    assert again.get("w") == (1, 1)


def test_client_budget_in_clients_toml(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "clients.toml").write_text(
        'schema_version = 1\n[clients.w]\nkind = "chat"\n'
        "[clients.w.budget]\nmax_requests_per_day = 100\nmax_tokens_per_day = 50000\n"
    )
    budget = load_config(paths).clients.clients["w"].budget
    assert (budget.max_requests_per_day, budget.max_tokens_per_day) == (100, 50000)


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
    spans: list[tuple[float, float]] = []

    async def slow(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/api/chat"):  # memory checks etc.
            return fake.handler(request)
        start = time.monotonic()
        await asyncio.sleep(0.3)
        spans.append((start, time.monotonic()))
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
    waits = sorted(p.details["queued_s"] for p in posts)
    assert waits[0] < waits[1] and waits[1] > 0  # the second waited for the first
    (_, e1), (s2, _) = sorted(spans)
    assert s2 >= e1  # the backend never served both at once
    await client.aclose()
    audit.close()


# ------------------------------------------------------------------ memory check (FR-GW-10)

GB = 1024**3


def test_memory_warning_cases() -> None:
    from cairnward.gateway.scheduling import memory_warning

    sizes = {"qwen3:30b": 18 * GB, "qwen3:1.7b": int(1.4 * GB)}
    # already loaded: nothing to say
    assert memory_warning("qwen3:30b", {"qwen3:30b": 18 * GB}, sizes, 2 * GB) is None
    # fits in free memory
    assert memory_warning("qwen3:1.7b", {}, sizes, 8 * GB) is None
    # the benchmark-day situation: 30B loaded, ~2 GB free, switch to the 1.7B model
    w = memory_warning("qwen3:1.7b", {"qwen3:30b": 18 * GB}, sizes, 1 * GB)
    assert w and "must unload qwen3:30b first" in w
    # wouldn't fit even after unloading everything
    w = memory_warning("qwen3:30b", {"qwen3:1.7b": int(1.4 * GB)}, sizes, 4 * GB)
    assert w and "heavy swapping" in w
    assert memory_warning("unknown", {}, sizes, 1 * GB) is None


async def test_gateway_reports_memory_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOllama()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3:30b", "size": 18 * GB}]})
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3:30b", "size": 18 * GB},
                        {"name": "qwen3:1.7b", "size": 2 * GB},
                    ]
                },
            )
        return fake.handler(request)

    monkeypatch.setattr("cairnward.gateway.app.meminfo", lambda: {"MemAvailable": 1 * GB})
    client, deps, audit, _ = await make(tmp_path, handler=handler)
    key = ClientKeys.at(deps.config.paths.state_dir).add("w")
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "cw/e:low", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 200
    assert "must unload qwen3:30b first" in resp.headers["x-cairnward-warning"]
    await client.aclose()
    audit.close()


# ------------------------------------------------------------------ llama.cpp slots (FR-GW-9)


async def test_slot_cache_saves_and_restores_per_conversation(tmp_path: Path) -> None:
    from tests.fakes.backends import FakeOpenAI

    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "e.toml").write_text('backend = "llama"\n')
    (paths.config_dir / "backends.toml").write_text(
        '[backends.llama]\ndriver = "openai_compat"\nurl = "http://127.0.0.1:8080/v1"\n'
        "slot_cache = true\n"
        '[roles.main]\nbackend = "llama"\nmodel = "m"\n'
        '[roles.fast]\nbackend = "llama"\nmodel = "m"\n'
    )
    cfg = load_config(paths)
    fake = FakeOpenAI()
    saved: set[str] = set()
    slot_calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slots/0":
            action = request.url.params["action"]
            name = json.loads(request.content)["filename"]
            slot_calls.append((action, name))
            if action == "save":
                saved.add(name)
                return httpx.Response(200, json={"n_saved": 100})
            return httpx.Response(200 if name in saved else 404, json={})
        return fake.handler(request)

    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(handler))
    with AuditLog.open(paths) as audit:
        deps = GatewayDeps(
            cfg, PolicyEngine(cfg, audit=audit), backends, ClientKeys.at(paths.state_dir)
        )
        app = create_app(deps)
        key = ClientKeys.at(paths.state_dir).add("w")
        h = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8484"
        ) as c:
            a1 = [{"role": "user", "content": "conversation A " * 50}]
            b1 = [{"role": "user", "content": "conversation B " * 50}]
            r = await c.post(
                "/v1/chat/completions", headers=h, json={"model": "cw/e:low", "messages": a1}
            )
            assert r.status_code == 200
            await c.post(
                "/v1/chat/completions", headers=h, json={"model": "cw/e:low", "messages": b1}
            )
            a2 = [*a1, r.json()["choices"][0]["message"], {"role": "user", "content": "more"}]
            await c.post(
                "/v1/chat/completions", headers=h, json={"model": "cw/e:low", "messages": a2}
            )
        actions = [a for a, _ in slot_calls]
        # A: restore attempt (nothing saved yet); B: save A, restore B (miss);
        # A again: save B, restore A (hit)
        assert actions == ["restore", "save", "restore", "save", "restore"]
        posts = [x for x in audit.records() if x.phase == "post"]
        assert [p.details.get("slot_restored") for p in posts] == [False, False, True]


def test_stream_guard_finds_think_split_across_pieces() -> None:
    """Real tokens split `</think>` (`</`, `think`, `>`): nothing is released until it's whole."""
    from cairnward.gateway.scheduling import StreamGuard

    g = StreamGuard()
    sent = [g.content(p) for p in ["Hmm, 2", "+2.\n</", "think", ">\n\nIt is", " 4."]]
    assert sent[:2] == [("", ""), ("", "")]
    assert sent[2] == ("", "")
    assert sent[3] == ("Hmm, 2+2.", "It is")
    assert sent[4] == ("", " 4.") and g.leaked and not g.holding


def test_stream_guard_releases_a_clean_answer_whole() -> None:
    from cairnward.gateway.scheduling import StreamGuard

    g = StreamGuard()
    assert [g.content(p) for p in ["It ", "is 4."]] == [("", ""), ("", "")]
    assert g.release() == "It is 4." and not g.leaked


def test_clean_models_are_remembered(tmp_path: Path) -> None:
    from cairnward.gateway.scheduling import ReasoningLeaks

    leaks = ReasoningLeaks.at(tmp_path)
    leaks.note_clean("m")
    leaks.note_clean("m")
    assert leaks.clean_models() == ["m"] and leaks.models() == []
