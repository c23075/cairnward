"""PaperQA2 inside Cairnward's server, for research jobs (FR-RES-13 with FR-RES-3).

The core issues the run key (budget, `run.start`/`run.end` in the audit log), maps the
answer and records it; the worker itself is started by whichever process may start
sandboxes:
- single-process mode: the server, in a thread (`LocalExecutor`);
- isolated mode: the core is sandboxed and srt can't nest, so the networked outer
  process runs it (`RemoteExecutor` -> egress control `/v1/engines/paperqa/run`). The
  core only names the library and the question; the outer process prepares the papers,
  the sandbox and the worker from its own configuration.
Every model call the worker makes goes through the gateway: queued, policy-checked and
audited like any client's.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from cairnward.config.loader import Config
from cairnward.gateway.scheduling import Budget
from cairnward.research.engines import paperqa

Executor = Callable[..., Awaitable[dict[str, Any]]]


def run_worker(cfg: Config, library: str, question: str, *, gateway_port: int, key: str,
               effort: str | None = None, evidence_k: int = 10, skip_summary: bool = False,
               log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:  # fmt: skip
    """Prepare from this process's own configuration and run the worker (blocking)."""
    prep = paperqa.prepare(cfg, library, effort)
    return paperqa.execute(cfg, prep, question, gateway_port=gateway_port, key=key,
                           evidence_k=evidence_k, skip_summary=skip_summary, log=log)  # fmt: skip


class LocalExecutor:
    def __init__(self, cfg: Config, log: Callable[[str], None] = lambda m: None):
        self.cfg, self.log = cfg, log

    async def __call__(self, library: str, question: str, **kw: Any) -> dict[str, Any]:
        return await asyncio.to_thread(run_worker, self.cfg, library, question, log=self.log,
                                       **kw)  # fmt: skip


class RemoteExecutor:
    def __init__(self, control_socket: str):
        self.socket = control_socket

    async def __call__(self, library: str, question: str, **kw: Any) -> dict[str, Any]:
        transport = httpx.AsyncHTTPTransport(uds=self.socket)
        async with httpx.AsyncClient(transport=transport, base_url="http://egress",
                                     timeout=httpx.Timeout(None, connect=10)) as c:  # fmt: skip
            resp = await c.post("/v1/engines/paperqa/run",
                                json={"library": library, "question": question, **kw})  # fmt: skip
        if resp.status_code != 200:
            raise paperqa.EngineError(resp.json().get("detail", resp.text))
        result: dict[str, Any] = resp.json()
        return result


class DeepEngine:
    def __init__(self, cfg: Config, *, run_keys: Any, executor: Executor, audit: Any = None):
        self.cfg, self.run_keys, self.executor, self.audit = cfg, run_keys, executor, audit

    async def ask(self, library: str, question: str, *, effort: str | None = None,
                  evidence_k: int = 10, skip_summary: bool = False,
                  actor: str = "cairnward") -> Any:  # fmt: skip
        prep = await asyncio.to_thread(paperqa.prepare, self.cfg, library, effort,
                                       create_work=False)  # fmt: skip
        env = self.cfg.environments[prep.environment]
        b = env.run_budget
        budget = Budget(max_requests=b.max_requests, max_tokens=b.max_tokens,
                        max_minutes=b.max_minutes)  # fmt: skip
        run_id = str(uuid.uuid4())
        key = self.run_keys.issue("paperqa", run_id, budget, prep.environment)
        self._event("run.start", prep, run_id, actor, details={
            "profile": "paperqa", "program": "paperqa", "sandbox": "outer process or server",
            "budget": {k: v for k, v in budget.__dict__.items() if v}})  # fmt: skip
        started, outcome = time.monotonic(), "error"
        try:
            result = await self.executor(
                library,
                question,
                effort=effort,
                evidence_k=evidence_k,
                skip_summary=skip_summary,
                key=key,
                gateway_port=self.cfg.main.server.gateway_port,
            )
            outcome = "ok"
        finally:
            self.run_keys.revoke_run(run_id)
            self._event("run.end", prep, run_id, actor, outcome=outcome,
                        details={"duration_s": round(time.monotonic() - started, 3)})  # fmt: skip
        answer = paperqa.finish(prep, question, result, time.monotonic() - started)
        if self.audit:
            paperqa.audit_answer(self.audit, prep, question, answer, run_id=run_id, actor=actor)
        return answer

    def _event(self, action: str, prep: Any, run_id: str, actor: str, **kw: Any) -> None:
        if self.audit:
            self.audit.append(action, actor=actor, environment=prep.environment, client="paperqa",
                              run_id=run_id, target="paperqa", **kw)  # fmt: skip
