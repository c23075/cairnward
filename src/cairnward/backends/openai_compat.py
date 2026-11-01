"""Driver for OpenAI-compatible servers: llama.cpp's llama-server, vLLM, SGLang, LocalAI,
hosted providers (Alibaba Model Studio, DeepSeek, OpenRouter)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from cairnward.backends.base import BackendError, BackendUnavailable, ChatRequest


class OpenAICompatDriver:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 900,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self.proxy = proxy  # None = direct (local backends)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout_s, connect=15),
            proxy=proxy,
            transport=transport,
            trust_env=False,  # never pick up proxy settings from the environment by accident
        )

    def _body(self, req: ChatRequest, stream: bool) -> dict[str, Any]:
        o = req.options
        body: dict[str, Any] = {**req.extra, "model": req.model, "messages": req.messages}
        for key, value in (("max_tokens", o.max_tokens), ("temperature", o.temperature),
                           ("top_p", o.top_p), ("top_k", o.top_k), ("min_p", o.min_p)):  # fmt: skip
            if value is not None:
                body[key] = value
        if o.thinking is not None:
            # Honoured by llama-server and vLLM for Qwen3-style templates.
            body["chat_template_kwargs"] = {
                **body.get("chat_template_kwargs", {}), "enable_thinking": o.thinking
            }  # fmt: skip
        if req.tools is not None:
            body["tools"] = req.tools
        if req.tool_choice is not None:
            body["tool_choice"] = req.tool_choice
        body["stream"] = stream
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error", {})
                msg = detail.get("message") if isinstance(detail, dict) else str(detail)
            except (ValueError, AttributeError):
                msg = resp.text[:300]
            raise BackendError(f"backend returned {resp.status_code}: {msg}", resp.status_code)

    async def chat(self, req: ChatRequest) -> dict[str, Any]:
        try:
            resp = await self._client.post("/chat/completions", json=self._body(req, False))
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        self._raise(resp)
        data: dict[str, Any] = resp.json()
        return data

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=self._body(req, True)
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._raise(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    yield json.loads(payload)
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    async def embed(self, model: str, inputs: list[str]) -> tuple[list[list[float]], int]:
        try:
            resp = await self._client.post("/embeddings", json={"model": model, "input": inputs})
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        self._raise(resp)
        data = resp.json()
        vectors = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        return vectors, int((data.get("usage") or {}).get("prompt_tokens") or 0)

    async def switch_slot(self, save_as: str | None, restore_from: str) -> bool:
        """llama-server: save slot 0's KV cache under `save_as`, then restore `restore_from`
        if it was saved before. Returns True if a saved cache was restored."""
        root = str(self._client.base_url).rstrip("/").removesuffix("/v1")
        try:
            if save_as:
                await self._client.post(f"{root}/slots/0?action=save", json={"filename": save_as})
            resp = await self._client.post(
                f"{root}/slots/0?action=restore", json={"filename": restore_from}
            )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def models(self) -> list[str]:
        try:
            resp = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{self.name}: {exc}") from exc
        self._raise(resp)
        return [m["id"] for m in resp.json().get("data", [])]

    async def health(self) -> bool:
        try:
            await self.models()
        except BackendError:
            return False
        return True

    async def aclose(self) -> None:
        await self._client.aclose()
