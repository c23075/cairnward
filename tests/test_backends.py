import json
from typing import Any

import httpx
import pytest

from cairnward.backends import (
    BackendError,
    Backends,
    BackendUnavailable,
    ChatRequest,
    GenerationOptions,
    OllamaDriver,
    OpenAICompatDriver,
    resolve_secret,
)
from cairnward.backends.ollama import to_ollama_messages
from cairnward.config.models import BackendsConfig
from tests.fakes.backends import FakeOllama, FakeOpenAI

MESSAGES = [
    {"role": "system", "content": "Be brief."},
    {"role": "user", "content": "What is 2+2?"},
]


def ollama(fake: FakeOllama) -> OllamaDriver:
    return OllamaDriver("local-ollama", "http://127.0.0.1:11434",
                        transport=httpx.MockTransport(fake.handler))  # fmt: skip


def openai(fake: FakeOpenAI, api_key: str | None = None) -> OpenAICompatDriver:
    return OpenAICompatDriver(
        "llama", "http://127.0.0.1:8080/v1", api_key=api_key,
        transport=httpx.MockTransport(fake.handler),
    )  # fmt: skip


# ------------------------------------------------------------------ ollama


async def test_ollama_chat_translates_options_and_reply() -> None:
    fake = FakeOllama(thinking="let me think")
    opts = GenerationOptions(thinking=False, context_tokens=8192, max_tokens=256,
                             temperature=0.2, keep_alive="10m")  # fmt: skip
    resp = await ollama(fake).chat(ChatRequest("qwen3:30b", MESSAGES, opts, extra={"seed": 7}))
    sent = fake.requests[0]
    assert sent["options"] == {"num_ctx": 8192, "num_predict": 256, "temperature": 0.2, "seed": 7}
    assert sent["think"] is False and sent["keep_alive"] == "10m" and sent["stream"] is False
    assert sent["messages"] == MESSAGES  # plain text messages pass through unchanged
    msg = resp["choices"][0]["message"]
    assert msg["content"] == "Hello from fake Ollama"
    assert msg["reasoning_content"] == "let me think"
    assert resp["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


async def test_ollama_stream() -> None:
    chunks = [c async for c in ollama(FakeOllama(reply="a b c")).chat_stream(
        ChatRequest("qwen3:30b", MESSAGES))]  # fmt: skip
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "a b c "
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 17


async def test_ollama_tool_calls_both_ways() -> None:
    fake = FakeOllama(tool_call={"name": "fs.read", "arguments": {"path": "a.py"}})
    resp = await ollama(fake).chat(ChatRequest("qwen3:30b", MESSAGES, tools=[{"type": "function"}]))
    call = resp["choices"][0]["message"]["tool_calls"][0]
    assert call["function"] == {"name": "fs.read", "arguments": '{"path": "a.py"}'}
    assert resp["choices"][0]["finish_reason"] == "tool_calls"
    assert fake.requests[0]["tools"] == [{"type": "function"}]
    stream = [c async for c in ollama(fake).chat_stream(ChatRequest("qwen3:30b", MESSAGES))]
    deltas = [c["choices"][0]["delta"] for c in stream if c["choices"]]
    assert any("tool_calls" in d for d in deltas)
    assert stream[-2]["choices"][0]["finish_reason"] == "tool_calls"


def test_message_translation_is_deterministic() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "fs.read", "arguments": '{"p": 1}'},
    }
    history: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "Describe"}, image]},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file text"},
    ]  # fmt: skip
    a, b = to_ollama_messages(history), to_ollama_messages(json.loads(json.dumps(history)))
    assert a == b
    assert a[0] == {"role": "user", "content": "Describe", "images": ["AAAA"]}
    assert a[1]["tool_calls"] == [{"function": {"name": "fs.read", "arguments": {"p": 1}}}]
    assert a[2] == {"role": "tool", "content": "file text", "tool_name": "fs.read"}


def test_remote_image_urls_rejected() -> None:
    with pytest.raises(BackendError):
        to_ollama_messages([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://x.example/a.png"}}]}])  # fmt: skip


async def test_ollama_errors() -> None:
    with pytest.raises(BackendError, match="404"):
        await ollama(FakeOllama(fail_status=404)).chat(ChatRequest("x", MESSAGES))

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    driver = OllamaDriver("o", "http://127.0.0.1:1", transport=httpx.MockTransport(down))
    with pytest.raises(BackendUnavailable):
        await driver.chat(ChatRequest("x", MESSAGES))
    assert await driver.health() is False


async def test_ollama_models_and_health() -> None:
    d = ollama(FakeOllama())
    assert await d.models() == ["qwen3:30b", "qwen3:1.7b"]
    assert await d.health()


# ------------------------------------------------------------------ openai-compatible


async def test_openai_passthrough_and_thinking_switch() -> None:
    fake = FakeOpenAI()
    opts = GenerationOptions(thinking=False, max_tokens=64, top_k=20)
    resp = await openai(fake, api_key="sk-test").chat(
        ChatRequest("qwen3-30b", MESSAGES, opts, extra={"stop": ["\n\n"]}))  # fmt: skip
    sent = fake.requests[0]
    assert sent["messages"] == MESSAGES and sent["stop"] == ["\n\n"]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert (sent["max_tokens"], sent["top_k"], sent["stream"]) == (64, 20, False)
    assert fake.headers[0]["authorization"] == "Bearer sk-test"
    assert resp["choices"][0]["message"]["content"] == "Hello from fake llama-server"


async def test_openai_stream_includes_usage() -> None:
    fake = FakeOpenAI(reply="x y")
    chunks = [c async for c in openai(fake).chat_stream(ChatRequest("m", MESSAGES))]
    assert fake.requests[0]["stream_options"] == {"include_usage": True}
    assert chunks[-1]["usage"]["total_tokens"] == 13


async def test_openai_error_message_surfaces() -> None:
    with pytest.raises(BackendError, match="503: overloaded"):
        await openai(FakeOpenAI(fail_status=503)).chat(ChatRequest("m", MESSAGES))


# ------------------------------------------------------------------ registry and secrets


def test_secret_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CW_TEST_KEY", "abc")
    assert resolve_secret("env:CW_TEST_KEY") == "abc"
    assert resolve_secret(None) is None
    monkeypatch.delenv("CW_TEST_KEY")
    with pytest.raises(BackendError, match="not set"):
        resolve_secret("env:CW_TEST_KEY")


async def test_registry_routes_remote_backends_through_egress() -> None:
    cfg = BackendsConfig.model_validate({
        "backends": {
            "local-ollama": {"driver": "ollama", "url": "http://127.0.0.1:11434"},
            "cloud": {"driver": "openai_compat", "url": "https://openrouter.ai/api/v1",
                      "location": "hosted"},
        },
        "roles": {"main": {"backend": "local-ollama", "model": "qwen3:30b"}},
    })  # fmt: skip
    reg = Backends(cfg, egress_url="http://cw:tok@127.0.0.1:8486")
    local, cloud = reg.get("local-ollama"), reg.get("cloud")
    assert reg.get("local-ollama") is local  # cached
    assert local.proxy is None  # type: ignore[attr-defined]
    assert cloud.proxy == "http://cw:tok@127.0.0.1:8486"  # type: ignore[attr-defined]
    with pytest.raises(BackendError):
        reg.get("missing")
    await reg.aclose()
