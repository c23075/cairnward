"""Driver for Ollama's native API (/api/chat).

The native API, not Ollama's OpenAI-compatible one, because only it sets the context
size (`num_ctx`, spec D60), the thinking switch (`think`) and `keep_alive`. Messages are
translated deterministically, so the same conversation always produces the same prompt
and Ollama's prefix cache keeps working (D62).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from cairnward.backends.base import BackendError, BackendUnavailable, ChatRequest

_FINISH = {"stop": "stop", "length": "length", "load": "stop", "unload": "stop"}


def _text_and_images(content: Any) -> tuple[str, list[str]]:
    """OpenAI content (string or parts) -> Ollama text + base64 images."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for part in content:
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif part.get("type") == "image_url":
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("data:") and "," in url:
                images.append(url.split(",", 1)[1])
            else:
                raise BackendError("Ollama needs images as data: URLs", 400)
    return "\n".join(texts), images


def to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tool_names: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        text, images = _text_and_images(m.get("content"))
        msg: dict[str, Any] = {"role": role, "content": text}
        if images:
            msg["images"] = images
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for call in m["tool_calls"]:
                fn = call.get("function", {})
                args = fn.get("arguments", "{}")
                try:
                    parsed = json.loads(args) if isinstance(args, str) else args
                except json.JSONDecodeError:
                    parsed = {"_raw": args}
                if call.get("id"):
                    tool_names[call["id"]] = fn.get("name", "")
                calls.append({"function": {"name": fn.get("name", ""), "arguments": parsed}})
            msg["tool_calls"] = calls
        if role == "tool":
            name = tool_names.get(m.get("tool_call_id", ""))
            if name:
                msg["tool_name"] = name
        if role == "assistant" and m.get("reasoning_content"):
            msg["thinking"] = m["reasoning_content"]
        out.append(msg)
    return out


def _tool_calls(raw: list[dict[str, Any]] | None, start: int = 0) -> list[dict[str, Any]]:
    calls = []
    for i, call in enumerate(raw or [], start=start):
        fn = call.get("function", {})
        args = fn.get("arguments", {})
        calls.append({
            "index": i,
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": fn.get("name", ""),
                "arguments": args if isinstance(args, str) else json.dumps(args),
            },
        })  # fmt: skip
    return calls


def _usage(data: dict[str, Any]) -> dict[str, int]:
    p, c = int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


class OllamaDriver:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        timeout_s: float = 900,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self.proxy = proxy  # None = direct (local backends)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s, connect=15),
            proxy=proxy,
            transport=transport,
            trust_env=False,
        )

    def _body(self, req: ChatRequest, stream: bool) -> dict[str, Any]:
        o = req.options
        options: dict[str, Any] = {}
        pairs = [
            ("num_ctx", o.context_tokens), ("num_predict", o.max_tokens),
            ("temperature", o.temperature), ("top_p", o.top_p), ("top_k", o.top_k),
            ("min_p", o.min_p), ("seed", req.extra.get("seed")), ("stop", req.extra.get("stop")),
        ]  # fmt: skip
        for key, value in pairs:
            if value is not None:
                options[key] = value
        body: dict[str, Any] = {
            "model": req.model,
            "messages": to_ollama_messages(req.messages),
            "stream": stream,
            "options": options,
        }
        if o.thinking is not None:
            body["think"] = o.thinking
        if o.keep_alive is not None:
            body["keep_alive"] = o.keep_alive
        if req.tools:
            body["tools"] = req.tools
        fmt = req.extra.get("response_format")
        if isinstance(fmt, dict) and fmt.get("type") == "json_object":
            body["format"] = "json"
        elif isinstance(fmt, dict) and fmt.get("type") == "json_schema":
            body["format"] = fmt.get("json_schema", {}).get("schema", "json")
        return body

    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error", resp.text[:300])
            except ValueError:
                msg = resp.text[:300]
            raise BackendError(f"ollama returned {resp.status_code}: {msg}", resp.status_code)

    async def chat(self, req: ChatRequest) -> dict[str, Any]:
        try:
            resp = await self._client.post("/api/chat", json=self._body(req, False))
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        self._raise(resp)
        data = resp.json()
        msg = data.get("message", {})
        calls = _tool_calls(msg.get("tool_calls"))
        out_msg: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or None}
        if msg.get("thinking"):
            out_msg["reasoning_content"] = msg["thinking"]
        if calls:
            out_msg["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in calls]
        finish = "tool_calls" if calls else _FINISH.get(data.get("done_reason", "stop"), "stop")
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [{"index": 0, "message": out_msg, "finish_reason": finish}],
            "usage": _usage(data),
        }

    async def chat_stream(self, req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        chunk_id, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {
                "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                "model": req.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }  # fmt: skip

        sent_role, n_calls = False, 0
        try:
            async with self._client.stream("POST", "/api/chat", json=self._body(req, True)) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._raise(resp)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "error" in data:
                        raise BackendError(f"ollama: {data['error']}")
                    msg = data.get("message", {})
                    delta: dict[str, Any] = {}
                    if not sent_role:
                        delta["role"], sent_role = "assistant", True
                    if msg.get("thinking"):
                        delta["reasoning_content"] = msg["thinking"]
                    if msg.get("content"):
                        delta["content"] = msg["content"]
                    if msg.get("tool_calls"):
                        delta["tool_calls"] = _tool_calls(msg["tool_calls"], n_calls)
                        n_calls += len(msg["tool_calls"])
                    if delta and (len(delta) > 1 or "role" not in delta or not data.get("done")):
                        yield chunk(delta)
                    if data.get("done"):
                        reason = data.get("done_reason", "stop")
                        finish = "tool_calls" if n_calls else _FINISH.get(reason, "stop")
                        yield chunk({}, finish)
                        yield {**chunk({}), "choices": [], "usage": _usage(data)}
                        return
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    async def models(self) -> list[str]:
        try:
            resp = await self._client.get("/api/tags")
        except httpx.HTTPError as exc:
            raise BackendUnavailable(f"{self.name}: {exc}") from exc
        self._raise(resp)
        return [m["name"] for m in resp.json().get("models", [])]

    async def health(self) -> bool:
        try:
            await self.models()
        except BackendError:
            return False
        return True

    async def aclose(self) -> None:
        await self._client.aclose()
