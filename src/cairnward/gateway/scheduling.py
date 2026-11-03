"""Request scheduling, budgets and waiting-time warnings (spec §4.1 D62; FR-GW-4/5/8/10/11).

On a CPU-only machine the model reads prompts slowly (~20 tok/s for a 30B MoE), and
re-reading an unchanged conversation start is avoidable: the server keeps the last
prompt's cache. So:
- requests to one backend are queued (max_parallel, default 1) instead of competing;
- when the slot frees up, a waiting request from the conversation the model just served
  goes first (its cache is warm), at most a few times in a row so others don't starve;
- each request's new (uncached) prompt tokens are estimated from the previous request on
  that backend, and turned into an expected wait using the stored benchmark profile;
- per-run budgets (requests, tokens, minutes) are enforced.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairnward.config.models import ClientBudget
from cairnward.gateway.recorder import normalise, prefix_hashes


def estimate_tokens(text: str) -> int:
    return max(1, -(-len(text) // 4))  # ~4 characters per token


AFFINITY_STREAK = 3  # a warm conversation may jump the queue at most this many times in a row
SLOW_SECONDS = 60


def message_tokens(messages: list[dict[str, Any]]) -> list[int]:
    """Rough token count per message (format overhead included)."""
    return [estimate_tokens(json.dumps(normalise(m), ensure_ascii=False)) + 4 for m in messages]


def conversation_key(messages: list[dict[str, Any]]) -> str:
    """Identifies a conversation by its first two messages (system prompt + first turn)."""
    hashes = prefix_hashes(messages[:2])
    return hashes[-1] if hashes else ""


# ------------------------------------------------------------------ benchmark profile


@dataclass(frozen=True)
class Speed:
    prompt_tps: float
    gen_tps: float | None


class BenchProfile:
    """Measured speeds per model, imported from scripts/bench_backend.py results."""

    def __init__(self, path: Path):
        self.path = path
        self.speeds: dict[str, Speed] = {}
        if path.exists():
            for model, s in json.loads(path.read_text(encoding="utf-8")).items():
                self.speeds[model] = Speed(float(s["prompt_tps"]), s.get("gen_tps"))

    @classmethod
    def at(cls, state_dir: Path) -> BenchProfile:
        return cls(state_dir / "bench_profile.json")

    def import_results(self, files: list[Path]) -> list[str]:
        imported = []
        for f in files:
            data = json.loads(f.read_text(encoding="utf-8"))
            summary = data.get("summary", {})
            if not summary.get("prompt_tps_median"):
                raise ValueError(f"{f}: no prompt speed in this benchmark result")
            self.speeds[data["model"]] = Speed(summary["prompt_tps_median"],
                                               summary.get("gen_tps_median"))  # fmt: skip
            imported.append(data["model"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({m: s.__dict__ for m, s in self.speeds.items()}, indent=2),
                             encoding="utf-8")  # fmt: skip
        return imported

    def expected_seconds(self, model: str, new_tokens: int) -> float | None:
        speed = self.speeds.get(model)
        return new_tokens / speed.prompt_tps if speed else None


class PromptSizes:
    """Each agent profile's first-prompt size, as measured in its runs (FR-AGT-1).

    Agents send a large system prompt and tool list first; on a CPU that first prompt
    decides how long the user waits, and it may not even fit the context."""

    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def at(cls, state_dir: Path) -> PromptSizes:
        return cls(state_dir / "prompt_sizes.json")

    def all(self) -> dict[str, dict[str, int]]:
        try:
            data: dict[str, dict[str, int]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data

    def note(self, profile: str, tokens: int) -> None:
        data = self.all()
        entry = data.get(profile, {"runs": 0, "max": 0})
        entry.update(last=tokens, runs=entry["runs"] + 1, max=max(entry["max"], tokens))
        data[profile] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, profile: str) -> int | None:
        entry = self.all().get(profile)
        return entry["last"] if entry else None


# ------------------------------------------------------------------ per-backend queue


@dataclass
class _Waiter:
    key: str
    future: asyncio.Future[None]


@dataclass
class _Backend:
    max_parallel: int
    active: int = 0
    waiters: list[_Waiter] = field(default_factory=list)
    last_key: str | None = None
    streak: int = 0
    # what the server last read (its cached prefix): message hashes and token counts
    cached_hashes: list[str] = field(default_factory=list)
    cached_tokens: list[int] = field(default_factory=list)
    served_key: str | None = None  # conversation whose prompt the server last read


@dataclass(frozen=True)
class Estimate:
    prompt_tokens: int
    cached_tokens: int

    @property
    def new_tokens(self) -> int:
        return self.prompt_tokens - self.cached_tokens


class Scheduler:
    def __init__(self, max_parallel: dict[str, int] | None = None):
        self._max = max_parallel or {}
        self._backends: dict[str, _Backend] = {}

    def _b(self, backend: str) -> _Backend:
        if backend not in self._backends:
            self._backends[backend] = _Backend(self._max.get(backend, 1))
        return self._backends[backend]

    def estimate(self, backend: str, messages: list[dict[str, Any]],
                 extra_tokens: int = 0) -> Estimate:  # fmt: skip
        """Prompt size and how much of it the server probably has cached already."""
        b = self._b(backend)
        hashes, tokens = prefix_hashes(messages), message_tokens(messages)
        common = 0
        for mine, theirs in zip(hashes, b.cached_hashes, strict=False):
            if mine != theirs:
                break
            common += 1
        total = sum(tokens) + extra_tokens
        cached = sum(tokens[:common]) + (extra_tokens if common else 0)
        return Estimate(total, min(cached, total))

    def served(self, backend: str, messages: list[dict[str, Any]]) -> None:
        b = self._b(backend)
        b.cached_hashes, b.cached_tokens = prefix_hashes(messages), message_tokens(messages)
        b.served_key = conversation_key(messages)

    def restored(self, backend: str, messages: list[dict[str, Any]]) -> None:
        """The server's cache was loaded from disk for this conversation (llama.cpp slots)."""
        self.served(backend, messages)

    def last_conversation(self, backend: str) -> str | None:
        return self._b(backend).served_key

    def queued(self, backend: str) -> int:
        b = self._b(backend)
        return len(b.waiters)

    @asynccontextmanager
    async def slot(self, backend: str, key: str) -> AsyncIterator[float]:
        """Hold one of the backend's slots; yields the seconds spent waiting."""
        b = self._b(backend)
        started = time.monotonic()
        if b.active < b.max_parallel and not b.waiters:
            b.active += 1
            self._note(b, key)
        else:
            waiter = _Waiter(key, asyncio.get_running_loop().create_future())
            b.waiters.append(waiter)
            try:
                await waiter.future  # the releaser counted us in `active` already
            except asyncio.CancelledError:
                if waiter in b.waiters:
                    b.waiters.remove(waiter)
                elif waiter.future.done() and not waiter.future.cancelled():
                    self._release(b)  # handed the slot just as we gave up
                raise
        try:
            yield time.monotonic() - started
        finally:
            self._release(b)

    @staticmethod
    def _note(b: _Backend, key: str) -> None:
        b.streak = b.streak + 1 if key == b.last_key else 1
        b.last_key = key

    def _release(self, b: _Backend) -> None:
        b.active -= 1
        if not b.waiters or b.active >= b.max_parallel:
            return
        pick = 0
        if b.streak < AFFINITY_STREAK:
            for i, w in enumerate(b.waiters):
                if w.key == b.last_key:
                    pick = i
                    break
        waiter = b.waiters.pop(pick)
        b.active += 1
        self._note(b, waiter.key)
        waiter.future.set_result(None)


# ------------------------------------------------------------------ run budgets


@dataclass(frozen=True)
class Budget:
    max_requests: int | None = None
    max_tokens: int | None = None
    max_minutes: float | None = None


@dataclass
class RunUsage:
    budget: Budget
    started: float = field(default_factory=time.monotonic)
    requests: int = 0
    tokens: int = 0

    def exceeded(self) -> str | None:
        b = self.budget
        if b.max_requests is not None and self.requests >= b.max_requests:
            return f"run budget: {b.max_requests} requests used"
        if b.max_tokens is not None and self.tokens >= b.max_tokens:
            return f"run budget: {b.max_tokens} tokens used"
        if b.max_minutes is not None and (time.monotonic() - self.started) / 60 >= b.max_minutes:
            return f"run budget: {b.max_minutes:g} minutes elapsed"
        return None


class ClientUsage:
    """Each client's requests and tokens today, for its daily budget (FR-GW-4). Kept in
    <state_dir>/client_usage.json so a restart doesn't reset the count; holds counts only."""

    def __init__(self, path: Path, today: Callable[[], str] = lambda: time.strftime("%Y-%m-%d")):
        self.path, self.today = path, today
        self._lock = threading.Lock()

    @classmethod
    def at(cls, state_dir: Path) -> ClientUsage:
        return cls(state_dir / "client_usage.json")

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data: dict[str, dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data

    def get(self, client: str) -> tuple[int, int]:
        """(requests, tokens) so far today."""
        entry = self._load().get(client)
        if not entry or entry.get("day") != self.today():
            return 0, 0
        return int(entry["requests"]), int(entry["tokens"])

    def exceeded(self, client: str, limits: ClientBudget) -> str | None:
        if limits.max_requests_per_day is None and limits.max_tokens_per_day is None:
            return None
        requests, tokens = self.get(client)
        if limits.max_requests_per_day is not None and requests >= limits.max_requests_per_day:
            return f"client budget: {limits.max_requests_per_day} requests used today"
        if limits.max_tokens_per_day is not None and tokens >= limits.max_tokens_per_day:
            return f"client budget: {limits.max_tokens_per_day} tokens used today"
        return None

    def add(self, client: str, tokens: int = 0) -> None:
        """Count one request."""
        with self._lock:
            requests, used = self.get(client)
            data = self._load()
            data[client] = {"day": self.today(), "requests": requests + 1, "tokens": used + tokens}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)  # atomic


def memory_warning(
    model: str, loaded: dict[str, int], sizes: dict[str, int], available: int
) -> str | None:
    """Warn when loading `model` would probably push the machine into swap (FR-GW-10)."""
    if not available or model in loaded or (f"{model}:latest" in loaded):
        return None
    size = sizes.get(model) or sizes.get(f"{model}:latest")
    if not size:
        return None
    need = size * 1.1  # model plus a modest KV cache
    if need <= available:
        return None
    gb = 1024**3
    others = ", ".join(sorted(loaded)) or "nothing"
    if need > available + sum(loaded.values()):
        return (
            f"loading {model} needs ~{need / gb:.1f} GB but only {available / gb:.1f} GB is "
            f"free even counting the loaded models ({others}): expect heavy swapping; "
            "close other apps"
        )
    return (
        f"loading {model} needs ~{need / gb:.1f} GB, {available / gb:.1f} GB free: the model "
        f"server must unload {others} first (OLLAMA_MAX_LOADED_MODELS=1), otherwise the "
        "machine will swap"
    )
