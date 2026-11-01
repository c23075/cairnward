"""Embeddings for the research library, called in-process with the same policy check and
audit records as the gateway's /v1/embeddings."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from cairnward.backends import Backends
from cairnward.config.loader import Config
from cairnward.gateway.models import resolve_embedding
from cairnward.policy.engine import Action, PolicyEngine


class EmbeddingDenied(Exception):
    pass


class Embedder(Protocol):
    model_name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class InProcessEmbedder:
    def __init__(
        self,
        cfg: Config,
        engine: PolicyEngine,
        backends: Backends,
        *,
        environment: str,
        actor: str = "cairnward",
        batch_size: int = 16,
    ):
        self.cfg, self.engine, self.backends = cfg, engine, backends
        self.environment, self.actor, self.batch_size = environment, actor, batch_size
        self.model_name = f"cw/{environment}:embed"
        self.resolved = resolve_embedding(cfg, self.model_name)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out += await self._batch(texts[i : i + self.batch_size])
        return out

    async def _batch(self, texts: list[str]) -> list[list[float]]:
        r = self.resolved
        action = Action(
            kind="model.call",
            target=r.backend,
            environment=self.environment,
            actor=self.actor,
            details={
                "model": r.model,
                "virtual_model": r.name,
                "operation": "embeddings",
                "inputs": len(texts),
            },
        )
        decision = await asyncio.to_thread(self.engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(self.engine.audit_deny, decision)
            raise EmbeddingDenied(decision.reason)
        pre = await asyncio.to_thread(self.engine.audit_pre, decision)
        started = time.monotonic()
        try:
            vectors, tokens = await self.backends.get(r.backend, self.environment).embed(
                r.model, texts
            )
        except Exception as exc:
            await asyncio.to_thread(
                self.engine.audit_post,
                action,
                pre,
                outcome="error",
                details={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )
            raise
        await asyncio.to_thread(
            self.engine.audit_post,
            action,
            pre,
            outcome="ok",
            details={"prompt_tokens": tokens, "duration_s": round(time.monotonic() - started, 3)},
        )
        return vectors
