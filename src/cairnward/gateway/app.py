"""The gateway: an OpenAI-compatible endpoint in front of the model backends (spec §4.1).

Per request: client key -> virtual model -> policy (`model.call`) -> audit pre ->
backend (messages untouched, FR-GW-7) -> vault (per privacy settings) -> audit post.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, field_validator

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
from cairnward.gateway.recorder import matching_conversation, record_exchange
from cairnward.gateway.scheduling import (
    SLOW_SECONDS,
    BenchProfile,
    ClientUsage,
    ReasoningLeaks,
    Scheduler,
    StreamGuard,
    conversation_key,
    estimate_tokens,
    memory_warning,
    split_leaked_reasoning,
)
from cairnward.policy.engine import Action, Decision, PolicyEngine
from cairnward.policy.provenance import ProvenanceTracker
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

    @field_validator("messages")
    @classmethod
    def _well_formed(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Checked, never changed: messages are forwarded as they came (FR-GW-7)."""
        for i, m in enumerate(messages):
            if not isinstance(m.get("role"), str) or not m["role"]:
                raise ValueError(f"messages[{i}] needs a 'role' (a string)")
            content = m.get("content")
            if not (
                content is None
                or isinstance(content, str)
                or (isinstance(content, list) and all(isinstance(p, dict) for p in content))
            ):
                raise ValueError(f"messages[{i}].content must be text, a list of parts or null")
            calls = m.get("tool_calls")
            if calls is not None and not (
                isinstance(calls, list) and all(isinstance(c, dict) for c in calls)
            ):
                raise ValueError(f"messages[{i}].tool_calls must be a list of objects")
        return messages


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
    provenance: ProvenanceTracker = field(default_factory=ProvenanceTracker)
    plugins: Any = None  # plugins.services.PluginSessions: the plugin services (ADR-0017)
    client_usage: ClientUsage | None = None
    reasoning_leaks: ReasoningLeaks | None = None


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


def _locked_state(vault: Vault, messages: list[dict[str, Any]]) -> str | None:
    """None, or "open"/"closed" when the request continues a locked conversation."""
    conv = matching_conversation(vault, messages)
    if conv is None or not vault.is_locked(conv):
        return None
    return "open" if vault.readable(conv) else "closed"


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
    if deps.plugins is not None:
        from cairnward.plugins.services import plugin_router

        app.include_router(plugin_router(cfg, deps.engine, deps.backends, deps.plugins,
                                         vault=deps.vault))  # fmt: skip

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
        """(client name, run id) for the presented key; None for a disabled client's keys."""
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        ident: tuple[str, str | None] | None = deps.run_keys.identify(key)
        if ident is None:
            name = deps.keys.identify(key)
            ident = (name, None) if name else None
        if ident and (known := cfg.clients.clients.get(ident[0])) and not known.enabled:
            return None
        return ident

    def client_of(request: Request) -> str | None:
        ident = identity(request)
        return ident[0] if ident else None

    def run_environment(run_id: str | None) -> str | None:
        return deps.run_keys.environment.get(run_id) if run_id else None

    async def outside_run_environment(
        resolved: Resolved, client: str, run_id: str | None
    ) -> JSONResponse | None:
        """A 403 if a run asks for an environment other than its own, audited: the environment
        also selects the privacy level (D57), so an agent must not choose its way out of it."""
        pinned = run_environment(run_id)
        if pinned is None or resolved.environment == pinned:
            return None
        why = f"this run is in '{pinned}', not '{resolved.environment}'"
        action = Action(
            kind="model.call",
            target=resolved.backend,
            environment=pinned,
            client=client,
            run_id=run_id,
            actor=f"agent:{client}",
            details={"virtual_model": resolved.name, "requested_environment": resolved.environment},
        )
        await asyncio.to_thread(deps.engine.audit_deny, Decision(action, "deny", "high", why))
        return _error(403, why, "run_environment")

    client_usage = deps.client_usage or ClientUsage.at(deps.config.paths.state_dir)
    reasoning_leaks = deps.reasoning_leaks or ReasoningLeaks.at(deps.config.paths.state_dir)

    async def client_over_budget(
        client: str, run_id: str | None, environment: str | None
    ) -> JSONResponse | None:
        """A 429 if this client has used its daily budget (FR-GW-4), audited."""
        client_cfg = deps.config.clients.clients.get(client)
        if client_cfg is None:
            return None
        why = await asyncio.to_thread(client_usage.exceeded, client, client_cfg.budget)
        if why is None:
            return None
        if deps.engine.audit:
            await asyncio.to_thread(
                deps.engine.audit.append,
                "budget.exceeded",
                actor=f"agent:{client}" if run_id else f"client:{client}",
                run_id=run_id,
                client=client,
                environment=environment,
                details={"reason": why},
            )
        return _error(429, f"{why}; change the limit in clients.toml", "budget_exceeded")

    async def count_for_client(client: str, tokens: int = 0) -> None:
        client_cfg = deps.config.clients.clients.get(client)
        limits = client_cfg.budget if client_cfg else None
        if limits and (limits.max_requests_per_day or limits.max_tokens_per_day):
            await asyncio.to_thread(client_usage.add, client, tokens)

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

        def owner(mid: str) -> str:
            """☁ marks virtual models that go to a hosted backend (FR-BACK-4)."""
            try:
                b = cfg.backends.backends[resolve(cfg, mid).backend]
            except (UnknownModel, KeyError):
                return "cairnward"
            if b.location != "hosted":
                return "cairnward"
            return "cairnward ☁ hosted" + (" (may train on prompts)" if b.trains_on_prompts else "")

        data = [
            {"id": mid, "object": "model", "created": 0, "owned_by": owner(mid)}
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
        default_env = run_environment(run_id) or (
            client_cfg.default_environment if client_cfg else None
        )
        try:
            resolved = resolve_embedding(cfg, body.model, default_environment=default_env)
        except UnknownModel as exc:
            return _error(404, str(exc), "invalid_request_error", "model_not_found")
        if refused := await outside_run_environment(resolved, client, run_id):
            return refused
        if over := await client_over_budget(client, run_id, resolved.environment):
            return over
        await count_for_client(client)
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
        default_env = run_environment(run_id) or (
            client_cfg.default_environment if client_cfg else None
        )
        try:
            resolved = resolve(cfg, body.model, default_environment=default_env, client=client)
        except UnknownModel as exc:
            return _error(404, str(exc), "invalid_request_error", "model_not_found")
        if refused := await outside_run_environment(resolved, client, run_id):
            return refused

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
        if over := await client_over_budget(client, run_id, resolved.environment):
            return over

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

        seen = deps.provenance.note_request(client, run_id, body.messages)
        action = Action(
            kind="model.call",
            target=resolved.backend,
            environment=resolved.environment,
            client=client,
            run_id=run_id,
            actor=f"agent:{client}" if run_id else f"client:{client}",
            provenance=seen,
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
        if deps.vault is not None and (locked := await asyncio.to_thread(
            _locked_state, deps.vault, body.messages
        )):  # fmt: skip
            if cfg.backends.backends[resolved.backend].location != "local":
                why = "locked conversations never go to hosted backends"
                denial = Decision(action, "deny", "high", why)
                await asyncio.to_thread(deps.engine.audit_deny, denial)
                return _error(403, f"this conversation is locked in Cairnward's vault: {why}",
                              "locked_conversation")  # fmt: skip
            if locked == "closed":
                warnings.append(
                    "this conversation is locked in the vault: this exchange won't be stored "
                    "until you unlock it (cairnward vault unlock)"
                )
        if blocked := await redact(
            resolved.backend, resolved.environment, list(message_texts(body.messages)),
            client, run_id, conversation_key(body.messages), warnings,
        ):  # fmt: skip
            return blocked

        extra = {k: v for k, v in (body.model_extra or {}).items() if k in PASSTHROUGH}
        options = _options(resolved, body)
        if options.thinking is False and resolved.model in await asyncio.to_thread(
            reasoning_leaks.models
        ):
            options = dataclasses.replace(options, thinking=True)
        chat_req = ChatRequest(
            model=resolved.model,
            messages=body.messages,
            options=options,
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

        def fallback_target(exc: BackendError) -> tuple[str, str] | None:
            """FR-BACK-3: retry on the backend's local fallback when it is unreachable or
            fails (5xx); client errors (4xx) aren't retried."""
            spec = cfg.backends.backends[resolved.backend]
            if not spec.fallback or 400 <= exc.status < 500:
                return None
            target = (spec.fallback, spec.fallback_model or resolved.model)
            slot_info["fallback"] = {"from": resolved.backend, "to": target[0],
                                     "model": target[1], "reason": str(exc)[:200]}  # fmt: skip
            if deps.engine.audit:
                deps.engine.audit.append(
                    "model.fallback", actor="cairnward", target=target[0],
                    environment=resolved.environment, client=client, run_id=run_id,
                    details=dict(slot_info["fallback"]),
                )  # fmt: skip
            return target

        async def chat_with_fallback(req: ChatRequest) -> dict[str, Any]:
            try:
                return await driver.chat(req)
            except BackendError as exc:
                target = fallback_target(exc)
                if target is None:
                    raise
                note = f"{resolved.backend} failed; answered by the local fallback {target[0]}"
                previous = headers.get("X-Cairnward-Warning")
                headers["X-Cairnward-Warning"] = f"{previous} | {note}" if previous else note
                fb = deps.backends.get(target[0], resolved.environment)
                return await fb.chat(dataclasses.replace(req, model=target[1]))

        async def stream_with_fallback(req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
            started_stream = False
            try:
                async for chunk in driver.chat_stream(req):
                    started_stream = True
                    yield chunk
            except BackendError as exc:
                target = None if started_stream else fallback_target(exc)
                if target is None:
                    raise
                fb = deps.backends.get(target[0], resolved.environment)
                async for chunk in fb.chat_stream(dataclasses.replace(req, model=target[1])):
                    yield chunk

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

        def separate_reasoning(reply: dict[str, Any]) -> None:
            """Move reasoning a model wrote into its answer to where it belongs (T-26)."""
            split = split_leaked_reasoning(reply.get("content") or "")
            if split is not None:
                reply["reasoning_content"] = f"{reply.get('reasoning_content') or ''}{split[0]}"
                reply["content"] = split[1] or None
                slot_info["reasoning_leak"] = True
                reasoning_leaks.note(resolved.model)

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
                if conv_id:
                    deps.provenance.note_conversation(client, run_id, conv_id)
                if rec is None:
                    slot_info["vault_note"] = "locked: not stored"
            if reply is not None:
                scheduler.served(resolved.backend, [*body.messages, reply])
                if usage_of_run is not None:
                    usage_of_run.requests += 1
                    usage_of_run.tokens += int(usage.get("total_tokens") or 0)
                await count_for_client(client, int(usage.get("total_tokens") or 0))
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
                    result = await chat_with_fallback(chat_req)
            except BackendError as exc:
                await finish(None, {}, str(exc)[:300])
                return _error(
                    502 if exc.status >= 500 or exc.status < 400 else exc.status,
                    str(exc),
                    "backend_error",
                )
            result["model"] = body.model
            reply = result["choices"][0]["message"]
            separate_reasoning(reply)
            await finish(reply, result.get("usage") or {}, None)
            return JSONResponse(result, headers=headers)

        # I5: a model not yet seen answering cleanly with thinking off may put reasoning in its
        # answer. Its first streamed reply is held until that's known (StreamGuard).
        guard_stream = options.thinking is False and resolved.model not in await asyncio.to_thread(
            reasoning_leaks.clean_models
        )

        async def events() -> AsyncIterator[bytes]:
            content: list[str] = []
            reasoning: list[str] = []
            calls: dict[int, dict[str, Any]] = {}
            usage: dict[str, Any] = {}
            error = None
            guards: dict[int, StreamGuard] = {}
            last: dict[str, Any] | None = None

            def guarded(choice: dict[str, Any]) -> None:
                """Apply the guard to one choice's delta, in place."""
                delta = choice.setdefault("delta", {})
                guard = guards.setdefault(choice.get("index", 0), StreamGuard())
                held_reasoning, answer = guard.content(delta.pop("content", None) or "")
                if choice.get("finish_reason") and guard.holding:
                    answer += guard.release()
                if held_reasoning:
                    delta["reasoning_content"] = held_reasoning + (
                        delta.get("reasoning_content") or ""
                    )
                if answer:
                    delta["content"] = answer

            try:
                async with scheduler.slot(resolved.backend, conv_key) as waited:
                    queued["s"] = waited
                    await switch_slot()
                    async for chunk in stream_with_fallback(chat_req):
                        chunk["model"] = body.model
                        last = chunk
                        for choice in chunk.get("choices") or []:
                            if guard_stream:
                                guarded(choice)
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
                    # a stream that ended without a finish signal: release what's still held
                    for index, guard in guards.items():
                        if guard.holding and (text := guard.release()):
                            content.append(text)
                            tail = {**(last or {}), "choices": [
                                {"index": index, "delta": {"content": text},
                                 "finish_reason": None}]}  # fmt: skip
                            tail.pop("usage", None)
                            yield f"data: {json.dumps(tail)}\n\n".encode()
                if guard_stream and guards and not any(g.leaked for g in guards.values()):
                    await asyncio.to_thread(reasoning_leaks.note_clean, resolved.model)
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
                    if any(g.leaked for g in guards.values()):
                        slot_info["reasoning_leak"] = True  # held back and separated (I5)
                        reasoning_leaks.note(resolved.model)
                    # a model already seen answering cleanly that leaks now: too late for this
                    # client, but it's asked for reasoning from the next call on
                    separate_reasoning(reply)
                await asyncio.shield(finish(reply, usage, error))

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    if deps.mcp is not None:
        from cairnward.mcp.http import add_mcp_routes

        def default_environment(client: str) -> str:
            c = cfg.clients.clients.get(client)
            return (c.default_environment if c else None) or cfg.main.defaults.environment

        def not_a_plugin(request: Request) -> tuple[str, str | None] | None:
            """A plugin's key is for its services: other plugins' tools would be capabilities it
            wasn't given, and its own output would look like a client's."""
            ident = identity(request)
            return None if ident and ident[1] and ident[1].startswith("plugin-") else ident

        add_mcp_routes(app, deps.mcp, not_a_plugin, default_environment=default_environment,
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
