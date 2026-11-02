"""OAuth for remote MCP servers (`auth = "oauth"` in mcp.toml), on the MCP SDK's client.

`cairnward mcp login <server>` signs in once, in a terminal: the SDK registers Cairnward
as a client (dynamic registration, PKCE), your browser shows the sign-in page, and the
answer comes back to a one-time listener on 127.0.0.1. The login's own requests go
through an egress session in the environment, like any helper program's. Tokens are
kept owner-only in `<state>/mcp-oauth/<server>.json` (never in config, never audited).

The running core only uses and refreshes those tokens; it never starts a sign-in. When
a token can't be refreshed, the server's tools fail with a message to log in again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from cairnward.config.paths import Paths


class LoginNeeded(Exception):
    pass


def token_file(paths: Paths, server: str) -> Path:
    return paths.state_dir / "mcp-oauth" / f"{server}.json"


class FileTokenStorage:
    """The SDK's TokenStorage in one owner-only JSON file per server."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.path.parent.chmod(0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)

    def redirect_uri(self) -> str | None:
        uris = (self._read().get("client") or {}).get("redirect_uris") or []
        return str(uris[0]) if uris else None

    def status(self) -> dict[str, Any]:
        data = self._read()
        tokens = data.get("tokens") or {}
        return {"signed_in": bool(tokens.get("access_token")),
                "can_refresh": bool(tokens.get("refresh_token")),
                "scope": tokens.get("scope")}  # fmt: skip

    def forget(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


def metadata(redirect_uri: str, scope: str | None) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="Cairnward", redirect_uris=[redirect_uri],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none", scope=scope,
    )  # fmt: skip


def provider_for_core(paths: Paths, server: str, url: str, scope: str | None) -> Any:
    """The core's provider: uses and refreshes stored tokens, never signs in."""
    storage = FileTokenStorage(token_file(paths, server))

    async def no_browser(_url: str) -> None:
        raise LoginNeeded(f"{server} needs you to sign in: cairnward mcp login {server}")

    redirect = storage.redirect_uri() or "http://127.0.0.1/callback"
    return OAuthClientProvider(url, metadata(redirect, scope), storage,
                               redirect_handler=no_browser)  # fmt: skip


class _Callback:
    """A one-time listener on 127.0.0.1 for the authorization server's redirect."""

    def __init__(self) -> None:
        self.result: asyncio.Future[AuthorizationCodeResult] | None = None
        self.server: asyncio.Server | None = None
        self.port = 0
        self.path = "/callback-" + secrets.token_urlsafe(8)

    async def start(self) -> str:
        self.result = asyncio.get_running_loop().create_future()
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}{self.path}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            target = line.split(" ")[1] if line.count(" ") >= 2 else ""
            parts = urlsplit(target)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            ok = parts.path == self.path and "code" in q
            body = ("Signed in. You can close this tab and go back to the terminal." if ok
                    else f"Sign-in failed: {q.get('error_description') or q.get('error') or 'no code'}")  # fmt: skip
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                         f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n"
                         f"{body}".encode())  # fmt: skip
            await writer.drain()
            if self.result is not None and not self.result.done() and parts.path == self.path:
                if ok:
                    self.result.set_result(AuthorizationCodeResult(
                        code=q["code"], state=q.get("state"), iss=q.get("iss")))  # fmt: skip
                else:
                    self.result.set_exception(LoginNeeded(body))
        except (TimeoutError, OSError, ValueError):
            pass
        finally:
            writer.close()

    async def wait(self, timeout: float) -> AuthorizationCodeResult:
        if self.result is None:
            raise RuntimeError("not started")
        try:
            return await asyncio.wait_for(self.result, timeout)
        except TimeoutError:
            raise LoginNeeded(f"no answer from the browser within {timeout:g} s") from None

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


async def login(paths: Paths, server: str, url: str, scope: str | None, *,
                headers: dict[str, str], proxy: str | None,
                open_url: Callable[[str], Awaitable[None] | None],
                timeout_s: float = 300) -> list[str]:  # fmt: skip
    """Sign in and return the server's tool names. Registers afresh for this listener's
    address, so an earlier registration (another port) doesn't matter."""
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    storage = FileTokenStorage(token_file(paths, server))
    storage.forget()
    callback = _Callback()
    redirect = await callback.start()

    async def redirect_handler(auth_url: str) -> None:
        maybe = open_url(auth_url)
        if maybe is not None:
            await maybe

    provider = OAuthClientProvider(
        url, metadata(redirect, scope), storage, redirect_handler=redirect_handler,
        callback_handler=lambda: callback.wait(timeout_s),
    )  # fmt: skip
    try:
        async with (
            httpx2.AsyncClient(auth=provider, headers=headers, proxy=proxy, trust_env=False,
                               timeout=httpx2.Timeout(30, read=timeout_s + 30)) as client,
            streamable_http_client(url, http_client=client) as (read, write),
            ClientSession(read, write) as session,
        ):  # fmt: skip
            await session.initialize()
            tools = await session.list_tools()
            return [t.name for t in tools.tools]
    finally:
        await callback.close()
