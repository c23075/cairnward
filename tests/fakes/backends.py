"""Fake model servers for tests (httpx MockTransport handlers)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeOllama:
    """Answers /api/chat like Ollama: echoes the last user message, optional tool call."""

    reply: str = "Hello from fake Ollama"
    tool_call: dict[str, Any] | None = None
    thinking: str | None = None
    fail_status: int | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "qwen3:30b"}, {"name": "qwen3:1.7b"}]}
            )
        body = json.loads(request.content)
        self.requests.append(body)
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": "model 'x' not found"})
        msg: dict[str, Any] = {"role": "assistant", "content": "" if self.tool_call else self.reply}
        if self.tool_call:
            msg["tool_calls"] = [{"function": self.tool_call}]
        if self.thinking:
            msg["thinking"] = self.thinking
        final = {"model": body["model"], "done": True, "done_reason": "stop",
                 "prompt_eval_count": 12, "eval_count": 5}  # fmt: skip
        if not body.get("stream"):
            return httpx.Response(200, json={**final, "message": msg})
        lines = []
        if self.thinking:
            think = {"role": "assistant", "content": "", "thinking": self.thinking}
            lines.append({"message": think,
                          "done": False})  # fmt: skip
        if self.tool_call:
            lines.append({"message": {"role": "assistant", "content": "",
                                      "tool_calls": msg["tool_calls"]}, "done": False})  # fmt: skip
        else:
            for word in self.reply.split(" "):
                lines.append(
                    {"message": {"role": "assistant", "content": word + " "}, "done": False}
                )
        lines.append({**final, "message": {"role": "assistant", "content": ""}})
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines) + "\n")


@dataclass
class FakeOpenAI:
    reply: str = "Hello from fake llama-server"
    fail_status: int | None = None
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(dict(request.headers))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-30b"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": {"message": "overloaded"}})
        usage = {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
        if not body.get("stream"):
            return httpx.Response(200, json={
                "id": "x", "object": "chat.completion", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": self.reply},
                             "finish_reason": "stop"}],
                "usage": usage,
            })  # fmt: skip
        events: list[dict[str, Any]] = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": w + " "}}]}
            for w in self.reply.split(" ")
        ]
        events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        events.append({"choices": [], "usage": usage})
        text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, content=text, headers={"content-type": "text/event-stream"})
