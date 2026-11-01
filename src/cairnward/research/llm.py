"""Model calls made by Cairnward's own research workflows (not by a client).

Same rules as gateway calls: the policy engine decides (`model.call`), the call is
audited (pre/post with digests, never content), redaction applies to non-local
backends, and inside the server it waits its turn in the gateway's per-backend queue so
research doesn't compete with chats for the CPU (D62).
"""

from __future__ import annotations

import asyncio
import re
import time
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Any

from cairnward.audit.log import canonical, digest
from cairnward.backends import Backends, ChatRequest
from cairnward.config.loader import Config
from cairnward.gateway.models import Resolved, resolve
from cairnward.policy.engine import Action, PolicyEngine
from cairnward.redaction import check_outbound, message_texts

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class ModelDenied(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    seconds: float


class InProcessChat:
    def __init__(
        self,
        cfg: Config,
        engine: PolicyEngine,
        backends: Backends,
        *,
        environment: str,
        effort: str | None = None,
        scheduler: Any = None,
        actor: str = "cairnward",
        purpose: str = "research",
    ):
        self.cfg, self.engine, self.backends = cfg, engine, backends
        self.environment, self.scheduler, self.actor, self.purpose = (
            environment, scheduler, actor, purpose)  # fmt: skip
        name = f"cw/{environment}:{effort}" if effort else f"cw/{environment}"
        self.resolved: Resolved = resolve(cfg, name)

    @property
    def context_tokens(self) -> int | None:
        return self.resolved.options.context_tokens

    async def ask(self, messages: list[dict[str, Any]], *, max_tokens: int = 1024) -> Reply:
        r = self.resolved
        action = Action(kind="model.call", target=r.backend, environment=self.environment,
                        actor=self.actor, details={"model": r.model, "virtual_model": r.name,
                                                   "operation": self.purpose})  # fmt: skip
        decision = await asyncio.to_thread(self.engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(self.engine.audit_deny, decision)
            raise ModelDenied(decision.reason)
        if self.cfg.backends.backends[r.backend].location != "local":
            verdict = await asyncio.to_thread(
                check_outbound, self.engine, list(message_texts(messages)),
                destination=f"backend:{r.backend}", environment=self.environment, client=None,
                initiated_by="cairnward",
            )  # fmt: skip
            if not verdict.allowed:
                raise ModelDenied(verdict.reason)
        params = canonical({"model": r.name, "messages": messages})
        pre = await asyncio.to_thread(self.engine.audit_pre, decision, params)
        o = r.options
        options = type(o)(**{**o.__dict__, "max_tokens": min(o.max_tokens or max_tokens,
                                                              max_tokens)})  # fmt: skip
        driver = self.backends.get(r.backend, self.environment)
        slot: AbstractAsyncContextManager[Any] = (
            self.scheduler.slot(r.backend, f"research:{self.purpose}")
            if self.scheduler is not None else nullcontext(0.0)
        )  # fmt: skip
        started = time.monotonic()
        try:
            async with slot:
                result = await driver.chat(ChatRequest(model=r.model, messages=messages,
                                                       options=options))  # fmt: skip
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="error",
                                    details={"error": error})  # fmt: skip
            raise
        msg = (result.get("choices") or [{}])[0].get("message") or {}
        text = _THINK.sub("", str(msg.get("content") or "")).strip()
        usage = result.get("usage") or {}
        seconds = time.monotonic() - started
        await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="ok",
                                result_digest=digest(text),
                                details={"prompt_tokens": usage.get("prompt_tokens"),
                                         "completion_tokens": usage.get("completion_tokens"),
                                         "duration_s": round(seconds, 3)})  # fmt: skip
        return Reply(text, r.model, usage.get("prompt_tokens"), usage.get("completion_tokens"),
                     seconds)  # fmt: skip
