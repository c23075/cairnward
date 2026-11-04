"""The services a plugin calls instead of holding a capability itself (ADR-0017, rule 3).

Every request carries a plugin key: held in memory, revoked when the plugin stops, and pinned
to one environment. The models service is the gateway's own endpoints. Here:

- fetch: an outside request. The core checks the destination as `net.connect` in the plugin's
  environment and scans the outgoing text with redaction, sends it through egress, records a
  digest, and returns the response bytes without parsing them. Redirects are returned, not
  followed, so each hop is checked as its own request.
- artifacts: durable output, in the plugin's own area (`plugin/<name>`) only.
- model info: what a plugin needs to size a request for a virtual model.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cairnward.artifacts import ArtifactError, ArtifactStore
from cairnward.audit.log import canonical, digest
from cairnward.config.loader import Config
from cairnward.egress.addresses import literal_address, special_reason
from cairnward.gateway.clients import RunKeys
from cairnward.plugins.contract import CONTRACT_VERSION
from cairnward.policy.engine import Action, PolicyEngine, host_matches
from cairnward.redaction import check_outbound

MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 128 * 1024 * 1024
RATE_WINDOW_S = 60.0
# Set by the core from the URL or by the HTTP client; a plugin choosing them could reach another
# site on a shared address, or smuggle a second request.
DROPPED_HEADERS = {"host", "connection", "content-length", "transfer-encoding", "te", "trailer",
                   "upgrade", "keep-alive", "proxy-authorization", "proxy-connection"}  # fmt: skip


@dataclass
class PluginSession:
    name: str
    environment: str
    run_id: str
    fetch_hosts: list[str]
    rate_per_minute: int
    recent: deque[float] = field(default_factory=deque)


class PluginSessions:
    """The running plugins' keys. A key is an ordinary run key to the gateway (audited and
    pinned to its environment), plus a session here for the plugin services."""

    def __init__(self, run_keys: RunKeys) -> None:
        self.run_keys = run_keys
        self._by_run: dict[str, PluginSession] = {}

    def issue(self, cfg: Config, name: str, environment: str) -> tuple[str, str]:
        """(key, run id)"""
        server = cfg.mcp.servers[name]
        run_id = f"plugin-{name}-{secrets.token_hex(8)}"
        key = self.run_keys.issue(f"plugin-{name}", run_id, None, environment)
        self._by_run[run_id] = PluginSession(name, environment, run_id, list(server.fetch_hosts),
                                             server.fetch_rate_per_minute)  # fmt: skip
        return key, run_id

    def revoke(self, run_id: str) -> None:
        self.run_keys.revoke_run(run_id)
        self._by_run.pop(run_id, None)

    def for_key(self, key: str | None) -> PluginSession | None:
        ident = self.run_keys.identify(key)
        return self._by_run.get(ident[1]) if ident else None


class FetchIn(BaseModel):
    method: str = Field(default="GET", pattern=r"^(GET|POST|HEAD)$")
    url: str = Field(max_length=4000)
    headers: dict[str, str] = Field(default_factory=dict)
    body_b64: str | None = Field(default=None, max_length=MAX_REQUEST * 2)
    timeout_s: float = Field(default=60, gt=0, le=600)


class ArtifactWriteIn(BaseModel):
    path: str = Field(max_length=500)
    content_b64: str = Field(max_length=8 * 1024 * 1024)
    message: str | None = Field(default=None, max_length=200)


def _b64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "not valid base64") from None


class ItemKeyIn(BaseModel):
    op: str = Field(pattern=r"^(status|lock|open|remove|recover)$")
    item: str = Field(max_length=200)
    secret: str | None = Field(default=None, max_length=4000, repr=False)


def plugin_router(cfg: Config, engine: PolicyEngine, backends: Any, sessions: PluginSessions,
                  *, transport: httpx.AsyncBaseTransport | None = None,
                  vault: Any = None) -> APIRouter:  # fmt: skip
    router = APIRouter(prefix="/v1/cairnward")
    store = ArtifactStore(cfg.paths.data_dir)

    def session_of(request: Request) -> PluginSession:
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        session = sessions.for_key(key)
        if session is None:
            raise HTTPException(403, "this service is for plugins, and needs a plugin's key")
        return session

    @router.get("/contract")
    def contract(request: Request) -> dict[str, Any]:
        s = session_of(request)
        return {"contract": CONTRACT_VERSION, "plugin": s.name, "environment": s.environment,
                "fetch_hosts": s.fetch_hosts}  # fmt: skip

    @router.get("/model-info")
    def model_info(request: Request, model: str) -> dict[str, Any]:
        from cairnward.gateway.models import resolve

        s = session_of(request)
        try:
            resolved = resolve(cfg, model)
        except Exception as exc:
            raise HTTPException(400, str(exc)[:300]) from None
        if resolved.environment != s.environment:
            raise HTTPException(403, f"this plugin is in '{s.environment}'")
        sub = cfg.effort[resolved.effort].subagents
        return {"context_tokens": resolved.options.context_tokens,
                "thinking": bool(resolved.options.thinking), "subagent_limit": sub.max,
                "subagent_parallel": sub.max_parallel}  # fmt: skip

    @router.post("/fetch")
    async def fetch(body: FetchIn, request: Request) -> dict[str, Any]:
        s = session_of(request)
        parts = urlsplit(body.url)
        host = parts.hostname or ""
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise HTTPException(400, "bad port") from None
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            raise HTTPException(400, "fetch takes an http or https URL, with no user name in it")
        target = f"{host}:{port}"

        def refuse(status: int, reason: str) -> HTTPException:
            action = Action(kind="net.connect", target=target, environment=s.environment,
                            client=f"plugin-{s.name}", run_id=s.run_id,
                            actor=f"plugin:{s.name}", details={"via": "plugin fetch"})  # fmt: skip
            if engine.audit:
                engine.audit.append("plugin.fetch", phase="event", decision="deny",
                                    actor=action.actor, target=target,
                                    environment=s.environment, client=action.client,
                                    run_id=s.run_id, details={"reason": reason[:200]})  # fmt: skip
            return HTTPException(status, reason)

        if not any(host_matches(target, p) or host_matches(host, p) for p in s.fetch_hosts):
            raise refuse(403, f"{host} isn't in {s.name}'s fetch_hosts")
        ip = literal_address(host.strip("[]"))
        allowed = cfg.privacy(s.environment).network_allow_hosts
        listed = any(host_matches(target, p) or host_matches(host, p) for p in allowed)
        if ip and special_reason(ip) and not listed:  # names are checked by egress when resolved
            raise refuse(403, f"{host} is this machine or a special address")
        now = time.monotonic()
        while s.recent and now - s.recent[0] > RATE_WINDOW_S:
            s.recent.popleft()
        if len(s.recent) >= s.rate_per_minute:
            raise HTTPException(429, f"{s.name} is limited to {s.rate_per_minute} fetches a minute")
        s.recent.append(now)

        content = _b64(body.body_b64) if body.body_b64 is not None else b""
        if len(content) > MAX_REQUEST:
            raise refuse(413, f"requests are limited to {MAX_REQUEST // 1024 // 1024} MB")
        headers = {k: v for k, v in body.headers.items() if k.lower() not in DROPPED_HEADERS}
        action = Action(kind="net.connect", target=target, environment=s.environment,
                        client=f"plugin-{s.name}", run_id=s.run_id, actor=f"plugin:{s.name}",
                        details={"via": "plugin fetch"})  # fmt: skip
        decision = await asyncio.to_thread(engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(engine.audit_deny, decision)
            raise HTTPException(403, f"{host} refused ({decision.reason})")
        texts = [body.url, *headers.values(), content.decode("utf-8", "replace")]
        verdict = await asyncio.to_thread(check_outbound, engine, texts, destination=host,
                                          environment=s.environment, client=f"plugin-{s.name}",
                                          run_id=s.run_id)  # fmt: skip
        if not verdict.allowed:
            raise HTTPException(403, f"not sent: {verdict.reason}")

        sent = {"method": body.method, "url": body.url, "body": content.decode("utf-8", "replace")}
        request_digest = digest(canonical(sent))
        proxy = backends.web_proxy(s.environment) if backends is not None else None
        kwargs: dict[str, Any] = {"transport": transport} if transport else {"proxy": proxy}
        status, reply_headers, data, error = 0, {}, b"", None
        try:
            async with (
                httpx.AsyncClient(timeout=body.timeout_s, follow_redirects=False, trust_env=False,
                                  **kwargs) as client,
                client.stream(body.method, body.url, headers=headers,
                              content=content or None) as resp,
            ):  # fmt: skip
                status, reply_headers = resp.status_code, dict(resp.headers)
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_raw():  # as sent: the plugin gets the bytes
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        error = f"the answer is over {MAX_RESPONSE // 1024 // 1024} MB"
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        if engine.audit:
            engine.audit.append("plugin.fetch", actor=f"plugin:{s.name}", target=target,
                                environment=s.environment, client=f"plugin-{s.name}",
                                run_id=s.run_id, outcome="error" if error else "ok",
                                details={"request_digest": request_digest, "method": body.method,
                                         "status": status, "bytes": len(data),
                                         "error": error})  # fmt: skip
        if error:
            raise HTTPException(502, error)
        return {"status": status, "headers": reply_headers, "warning": verdict.warning,
                "body_b64": base64.b64encode(data).decode()}  # fmt: skip

    @router.post("/item-keys")
    def item_keys(body: ItemKeyIn, request: Request) -> dict[str, Any]:
        """A plugin's item key, stored wrapped with the conversations' keys so the recovery key
        covers it. Opening one takes the passphrase. Names are kept apart per plugin, except
        `library:` keys, which research libraries locked before the split already use."""
        from cairnward.store.crypto import DecryptionError
        from cairnward.store.vault import LockedError, VaultError

        s = session_of(request)
        if vault is None:
            raise HTTPException(503, "no vault in this process")
        item = f"plugin:{s.name}:{body.item}"
        if body.item.startswith("library:") and vault.is_locked(body.item):
            item = body.item
        try:
            if body.op == "status":
                return {"locked": vault.is_locked(item)}
            if body.op == "remove":
                vault.remove_item(item)
                outcome: dict[str, Any] = {"ok": True}
            elif body.op == "recover":
                identity, _, secret = (body.secret or "").partition("\n")
                vault.recover(item, identity, secret)
                outcome = {"ok": True}
            else:
                fn = vault.lock_item if body.op == "lock" else vault.open_item
                key = fn(item, body.secret or "")
                outcome = {"key_b64": base64.b64encode(key).decode()}
        except (VaultError, LockedError, DecryptionError) as exc:
            if engine.audit:
                engine.audit.append("plugin.item_key", actor=f"plugin:{s.name}", target=item,
                                    environment=s.environment, outcome="error",
                                    details={"op": body.op})  # fmt: skip
            raise HTTPException(400, str(exc)) from None
        if engine.audit:
            engine.audit.append("plugin.item_key", actor=f"plugin:{s.name}", target=item,
                                environment=s.environment, details={"op": body.op})  # fmt: skip
        return outcome

    def artifact_call(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except ArtifactError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.post("/artifacts")
    async def artifact_write(body: ArtifactWriteIn, request: Request) -> dict[str, str]:
        s = session_of(request)
        rev = await asyncio.to_thread(artifact_call, store.write, "plugin", s.name, body.path,
                                      _b64(body.content_b64), body.message)  # fmt: skip
        if engine.audit:
            engine.audit.append("artifact.write", actor=f"plugin:{s.name}", run_id=s.run_id,
                                target=f"plugin/{s.name}", environment=s.environment,
                                details={"rev": rev})  # fmt: skip
        return {"rev": rev}

    @router.get("/artifacts")
    async def artifact_list(request: Request) -> dict[str, list[str]]:
        s = session_of(request)
        if not store.repo("plugin", s.name).exists():
            return {"files": []}
        return {"files": await asyncio.to_thread(artifact_call, store.files, "plugin", s.name)}

    @router.get("/artifacts/content")
    async def artifact_read(request: Request, path: str,
                            rev: str | None = None) -> dict[str, str]:  # fmt: skip
        s = session_of(request)
        data = await asyncio.to_thread(artifact_call, store.read, "plugin", s.name, path, rev)
        return {"content_b64": base64.b64encode(data).decode()}

    return router
