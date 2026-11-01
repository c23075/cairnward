"""The gateway: an OpenAI-compatible endpoint in front of the model backends (spec §4.1).

Per request: client key -> virtual model -> policy (`model.call`) -> audit pre ->
backend (messages untouched, FR-GW-7) -> vault (per privacy settings) -> audit post.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict

from cairnward import __version__
from cairnward.audit.log import canonical, digest
from cairnward.backends import BackendError, Backends, ChatRequest, GenerationOptions
from cairnward.config.loader import Config
from cairnward.config.models import LOOPBACK_HOSTS
from cairnward.gateway.clients import ClientKeys, RunKeys
from cairnward.gateway.models import (
    Resolved,
    UnknownModel,
    resolve,
    resolve_embedding,
    virtual_model_ids,
)
from cairnward.gateway.recorder import record_exchange
from cairnward.gateway.scheduling import (
    SLOW_SECONDS,
    BenchProfile,
    Scheduler,
    conversation_key,
    estimate_tokens,
    memory_warning,
)
from cairnward.policy.engine import Action, PolicyEngine
from cairnward.redaction import check_outbound, message_texts
from cairnward.store import Vault
from cairnward.sysinfo import meminfo

PASSTHROUGH = (
    "stop",
    "seed",
    "response_format",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "user",
    "parallel_tool_calls",
)


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    input: str | list[str]


class ChatCompletionRequest(BaseModel):
    """The OpenAI chat-completions request (unknown fields are accepted and ignored)."""

    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None


def _error(status: int, message: str, kind: str, code: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": kind, "code": code or kind}},
    )


@dataclass
class GatewayDeps:
    config: Config
    engine: PolicyEngine
    backends: Backends
    keys: ClientKeys
    vault: Vault | None = None
    run_keys: RunKeys = field(default_factory=RunKeys)
    scheduler: Scheduler | None = None
    profile: BenchProfile | None = None
    mcp: Any = None  # McpHub: the MCP proxy (spec §4.2), when configured


def _options(resolved: Resolved, req: ChatCompletionRequest) -> GenerationOptions:
    o = resolved.options
    client_max = req.max_completion_tokens or req.max_tokens
    if o.max_tokens and client_max:
        max_tokens: int | None = min(o.max_tokens, client_max)
    else:
        max_tokens = o.max_tokens or client_max
    return GenerationOptions(
        thinking=o.thinking,
        context_tokens=o.context_tokens,
        max_tokens=max_tokens,
        temperature=o.temperature if o.temperature is not None else req.temperature,
        top_p=o.top_p if o.top_p is not None else req.top_p,
        top_k=o.top_k,
        min_p=o.min_p,
        keep_alive="10m",
    )


def create_app(deps: GatewayDeps) -> FastAPI:
    cfg = deps.config
    if deps.scheduler is None:
        deps.scheduler = Scheduler({n: b.max_parallel for n, b in cfg.backends.backends.items()})
    scheduler = deps.scheduler
    port = cfg.main.server.gateway_port
    app = FastAPI(
        title="Cairnward gateway",
        version=__version__,
        description="OpenAI-compatible endpoint. Every call is policy-checked and audited.",
    )

    app.state.deps = deps

    @app.middleware("http")
    async def loopback_host_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # DNS-rebinding defence: a web page can't reach us under a foreign Host name.
        host = request.headers.get("host", "")
        # "[::1]:8484" -> "[::1]"; "127.0.0.1:8484" -> "127.0.0.1"
        name = host[: host.find("]") + 1] if host.startswith("[") else host.split(":", 1)[0]
        if name.lower() not in LOOPBACK_HOSTS:
            return _error(421, f"host '{host}' not accepted; use 127.0.0.1:{port}", "bad_host")
        return await call_next(request)

    def identity(request: Request) -> tuple[str, str | None] | None:
        """(client name, run id) for the presented key."""
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        if run := deps.run_keys.identify(key):
            return run
        name = deps.keys.identify(key)
        return (name, None) if name else None

    def client_of(request: Request) -> str | None:
        ident = identity(request)
        return ident[0] if ident else None

    async def redact(
        backend: str,
        environment: str,
        texts: list[str],
        client: str,
        run_id: str | None,
        conversation: str | None,
        warnings: list[str],
    ) -> JSONResponse | None:
        """Redaction for content leaving the machine (non-local backends, spec §5.4)."""
        if cfg.backends.backends[backend].location == "local":
            return None
        verdict = await asyncio.to_thread(
            check_outbound, deps.engine, texts, destination=f"backend:{backend}",
            environment=environment, client=client, run_id=run_id, conversation_id=conversation,
        )  # fmt: skip
        if not verdict.allowed:
            return _error(403, f"withheld by cairnward redaction: {verdict.reason}",
                          "redaction_blocked")  # fmt: skip
        if verdict.warning:
            warnings.append(verdict.warning)
        return None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/models", response_model=None)
    async def list_models(request: Request) -> dict[str, Any] | JSONResponse:
        if client_of(request) is None:
            return _error(401, "missing or unknown API key (cairnward client add <name>)", "auth")
        data = [
            {"id": mid, "object": "model", "created": 0, "owned_by": "cairnward"}
            for mid in virtual_model_ids(cfg)
        ]
        return {"object": "list", "data": data}

    @app.post("/v1/embeddings", response_model=None)
    async def embeddings(body: EmbeddingRequest, request: Request) -> dict[str, Any] | JSONResponse:
        ident = identity(request)
        if ident is None:
            return _error(401, "missing or unknown API key (cairnward client add <name>)", "auth")
        client, run_id = ident
        if run_id and (run_use := deps.run_keys.usage.get(run_id)):
            if why := run_use.exceeded():
                return _error(429, why, "budget_exceeded")
            run_use.requests += 1
        client_cfg = cfg.clients.clients.get(client)
        default_env = client_cfg.default_environment if client_cfg else None
        try:
            resolved = resolve_embedding(cfg, body.model, default_environment=default_env)
        except UnknownModel as exc:
            return _error(404, str(exc), "invalid_request_error", "model_not_found")
        inputs = [body.input] if isinstance(body.input, str) else list(body.input)
        action = Action(
            kind="model.call",
            target=resolved.backend,
            environment=resolved.environment,
            client=client,
            run_id=run_id,
            actor=f"agent:{client}" if run_id else f"client:{client}",
            details={
                "model": resolved.model,
                "virtual_model": resolved.name,
                "operation": "embeddings",
                "inputs": len(inputs),
            },
        )
        decision = await asyncio.to_thread(deps.engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(deps.engine.audit_deny, decision)
            return _error(403, f"denied by cairnward policy: {decision.reason}", "policy_denied")
        if blocked := await redact(
            resolved.backend, resolved.environment, inputs, client, run_id, None, []
        ):
            return blocked
        pre_seq = await asyncio.to_thread(deps.engine.audit_pre, decision, canonical(inputs))
        started = time.monotonic()
        driver = deps.backends.get(resolved.backend, resolved.environment)
        try:
            async with scheduler.slot(resolved.backend, "embed"):
                vectors, tokens = await driver.embed(resolved.model, inputs)
        except BackendError as exc:
            await asyncio.to_thread(
                deps.engine.audit_post,
                action,
                pre_seq,
                outcome="error",
                details={"error": str(exc)[:300]},
            )
            return _error(502, str(exc), "backend_error")
        await asyncio.to_thread(
            deps.engine.audit_post,
            action,
            pre_seq,
            outcome="ok",
            details={"prompt_tokens": tokens, "duration_s": round(time.monotonic() - started, 3)},
        )
        return {
            "object": "list",
            "model": body.model,
            "data": [
                {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
            ],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        }

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        body: ChatCompletionRequest, request: Request
    ) -> dict[str, Any] | JSONResponse | StreamingResponse:
        ident = identity(request)
        if ident is None:
            return _error(401, "missing or unknown API key (cairnward client add <name>)", "auth")
        client, run_id = ident
        client_cfg = cfg.clients.clients.get(client)
        default_env = client_cfg.default_environment if client_cfg else None
        try:
            resolved = resolve(cfg, body.model, default_environment=default_env)
        except UnknownModel as exc:
            return _error(404, str(exc), "invalid_request_error", "model_not_found")

        usage_of_run = deps.run_keys.usage.get(run_id) if run_id else None
        if usage_of_run and (why := usage_of_run.exceeded()):
            if deps.engine.audit:
                await asyncio.to_thread(
                    deps.engine.audit.append,
                    "budget.exceeded",
                    actor=f"agent:{client}",
                    run_id=run_id,
                    client=client,
                    environment=resolved.environment,
                    details={"reason": why},
                )
            return _error(
                429, f"{why}; raise the limit with cairnward run --max-...", "budget_exceeded"
            )

        tool_tokens = estimate_tokens(json.dumps(body.tools)) if body.tools else 0
        est = scheduler.estimate(resolved.backend, body.messages, tool_tokens)
        warnings: list[str] = []
        ctx = resolved.options.context_tokens
        env_cfg = cfg.environments[resolved.environment]
        if ctx and est.prompt_tokens > ctx:
            msg = (
                f"prompt is about {est.prompt_tokens} tokens but the context is {ctx}; "
                "the model server would drop the start of the conversation"
            )
            if env_cfg.context_overflow == "reject":
                return _error(
                    400,
                    msg + " (context_overflow = reject)",
                    "invalid_request_error",
                    "context_length_exceeded",
                )
            warnings.append(msg)
        expected = (
            deps.profile.expected_seconds(resolved.model, est.new_tokens) if deps.profile else None
        )
        if expected and expected > SLOW_SECONDS:
            warnings.append(
                f"about {est.new_tokens} new prompt tokens: roughly {expected / 60:.0f} "
                f"min before the first word on {resolved.model}"
            )

        driver_for_memory = deps.backends.get(resolved.backend, resolved.environment)
        view = getattr(driver_for_memory, "memory_view", None)
        if view is not None and (avail := meminfo().get("MemAvailable", 0)):
            loaded, sizes = await view()
            if mem := memory_warning(resolved.model, loaded, sizes, avail):
                warnings.append(mem)

        action = Action(
            kind="model.call",
            target=resolved.backend,
            environment=resolved.environment,
            client=client,
            run_id=run_id,
            actor=f"agent:{client}" if run_id else f"client:{client}",
            details={
                "model": resolved.model,
                "virtual_model": resolved.name,
                "effort": resolved.effort,
                "stream": body.stream,
                "est_prompt_tokens": est.prompt_tokens,
                "est_new_tokens": est.new_tokens,
                "expected_wait_s": round(expected) if expected else None,
                "warnings": warnings,
            },
        )
        decision = await asyncio.to_thread(deps.engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(deps.engine.audit_deny, decision)
            return _error(403, f"denied by cairnward policy: {decision.reason}", "policy_denied")
        if blocked := await redact(
            resolved.backend, resolved.environment, list(message_texts(body.messages)),
            client, run_id, conversation_key(body.messages), warnings,
        ):  # fmt: skip
            return blocked

        extra = {k: v for k, v in (body.model_extra or {}).items() if k in PASSTHROUGH}
        chat_req = ChatRequest(
            model=resolved.model,
            messages=body.messages,
            options=_options(resolved, body),
            tools=body.tools,
            tool_choice=body.tool_choice,
            extra=extra,
        )
        params = canonical({"model": body.model, "messages": body.messages, "tools": body.tools})
        pre_seq = await asyncio.to_thread(deps.engine.audit_pre, decision, params)
        driver = deps.backends.get(resolved.backend, resolved.environment)
        started = time.monotonic()
        privacy = cfg.privacy(resolved.environment)
        conv_key = conversation_key(body.messages)
        slot_info: dict[str, Any] = {}

        async def switch_slot() -> None:
            """llama.cpp: swap the KV cache to this conversation's saved one (FR-GW-9)."""
            if not deps.backends.spec(resolved.backend).slot_cache:
                return
            prev = scheduler.last_conversation(resolved.backend)
            if prev == conv_key:
                return
            switch = getattr(driver, "switch_slot", None)
            if switch is None:
                return
            restored = await switch(
                f"cw-{prev[:32]}.bin" if prev else None, f"cw-{conv_key[:32]}.bin"
            )
            slot_info["slot_restored"] = restored
            if restored:  # the server now holds this conversation up to its previous turn
                scheduler.restored(resolved.backend, body.messages[:-1])

        queued: dict[str, float] = {"s": 0.0}
        headers = {"Cache-Control": "no-store"}
        if warnings:
            headers["X-Cairnward-Warning"] = " | ".join(warnings)

        async def finish(
            reply: dict[str, Any] | None, usage: dict[str, Any], error: str | None
        ) -> str | None:
            conv_id = None
            if reply is not None and deps.vault is not None and privacy.value("vault") == "full":
                rec = await asyncio.to_thread(
                    record_exchange,
                    deps.vault,
                    body.messages,
                    reply,
                    environment=resolved.environment,
                    client=client,
                    model=resolved.model,
                    effort=resolved.effort,
                    crypto_shred=privacy.value("crypto_shred") == "on",
                    completion_tokens=usage.get("completion_tokens"),
                )
                conv_id = rec.conversation_id if rec else None
            if reply is not None:
                scheduler.served(resolved.backend, [*body.messages, reply])
                if usage_of_run is not None:
                    usage_of_run.requests += 1
                    usage_of_run.tokens += int(usage.get("total_tokens") or 0)
            details: dict[str, Any] = {
                **slot_info,
                "queued_s": round(queued["s"], 3),
                "duration_s": round(time.monotonic() - started, 3),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "vault": privacy.value("vault"),
            }
            if conv_id:
                details["conversation"] = conv_id
            if error:
                details["error"] = error
            await asyncio.to_thread(
                deps.engine.audit_post,
                action,
                pre_seq,
                outcome="error" if error else "ok",
                result_digest=digest(canonical(reply)) if reply else None,
                details=details,
            )
            return conv_id

        if not body.stream:
            try:
                async with scheduler.slot(resolved.backend, conv_key) as waited:
                    queued["s"] = waited
                    await switch_slot()
                    result = await driver.chat(chat_req)
            except BackendError as exc:
                await finish(None, {}, str(exc)[:300])
                return _error(
                    502 if exc.status >= 500 or exc.status < 400 else exc.status,
                    str(exc),
                    "backend_error",
                )
            result["model"] = body.model
            reply = result["choices"][0]["message"]
            await finish(reply, result.get("usage") or {}, None)
            return JSONResponse(result, headers=headers)

        async def events() -> AsyncIterator[bytes]:
            content: list[str] = []
            reasoning: list[str] = []
            calls: dict[int, dict[str, Any]] = {}
            usage: dict[str, Any] = {}
            error = None
            try:
                async with scheduler.slot(resolved.backend, conv_key) as waited:
                    queued["s"] = waited
                    await switch_slot()
                    async for chunk in driver.chat_stream(chat_req):
                        chunk["model"] = body.model
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                content.append(delta["content"])
                            if delta.get("reasoning_content"):
                                reasoning.append(delta["reasoning_content"])
                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(
                                    tc.get("index", 0),
                                    {
                                        "id": tc.get("id"),
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    },
                                )
                                fn = tc.get("function") or {}
                                slot["id"] = slot["id"] or tc.get("id")
                                slot["function"]["name"] += fn.get("name") or ""
                                slot["function"]["arguments"] += fn.get("arguments") or ""
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        yield f"data: {json.dumps(chunk)}\n\n".encode()
                yield b"data: [DONE]\n\n"
            except BackendError as exc:
                error = str(exc)[:300]
                err = {"error": {"message": error, "type": "backend_error"}}
                yield f"data: {json.dumps(err)}\n\n".encode()
            except asyncio.CancelledError:
                error = "client disconnected"
                raise
            finally:
                reply: dict[str, Any] | None = None
                if error is None:
                    reply = {"role": "assistant", "content": "".join(content) or None}
                    if calls:
                        reply["tool_calls"] = [calls[i] for i in sorted(calls)]
                    if reasoning:
                        reply["reasoning_content"] = "".join(reasoning)
                await asyncio.shield(finish(reply, usage, error))

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    if deps.mcp is not None:
        from cairnward.mcp.http import add_mcp_routes

        def default_environment(client: str) -> str:
            c = cfg.clients.clients.get(client)
            return (c.default_environment if c else None) or cfg.main.defaults.environment

        add_mcp_routes(app, deps.mcp, identity, default_environment=default_environment,
                       run_environment=deps.run_keys.environment.get)  # fmt: skip

    return app


def openapi_document() -> dict[str, Any]:
    """The gateway's OpenAPI document, built from default settings (contract, FR-API-4)."""
    import tempfile
    from pathlib import Path

    from cairnward.config.loader import load_config
    from cairnward.config.paths import Paths

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        cfg = load_config(Paths(base / "cfg", base / "data", base / "state"))
        deps = GatewayDeps(cfg, PolicyEngine(cfg), Backends(cfg.backends), ClientKeys.at(base))
        doc: dict[str, Any] = create_app(deps).openapi()
    return doc
