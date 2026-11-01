"""Compatibility with the official OpenAI Python SDK, which speaks the same API that
Open WebUI, Jan, Aider (via litellm) and most agents use (T0.15, FR-CLI-1)."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import openai
import pytest

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.gateway import ClientKeys, GatewayDeps, create_app
from cairnward.policy import PolicyEngine
from cairnward.store import Vault
from tests.fakes.backends import FakeOllama


@pytest.fixture
async def sdk(tmp_path: Path) -> AsyncIterator[tuple[openai.AsyncOpenAI, FakeOllama, Vault]]:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    fake = FakeOllama(reply="Hi from Cairnward")
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fake.handler))
    with AuditLog.open(paths) as audit, Vault.open(paths) as vault:
        keys = ClientKeys.at(paths.state_dir)
        key = keys.add("open-webui")
        app = create_app(GatewayDeps(cfg, PolicyEngine(cfg, audit=audit), backends, keys, vault))
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        # The SDK types its client as its own httpx fork; an httpx client works at runtime.
        client = openai.AsyncOpenAI(
            base_url="http://127.0.0.1:8484/v1",
            api_key=key,
            http_client=http,  # type: ignore[arg-type]
        )
        yield client, fake, vault
        await http.aclose()


async def test_models_list(sdk: tuple[openai.AsyncOpenAI, FakeOllama, Vault]) -> None:
    client, _, _ = sdk
    ids = [m.id async for m in client.models.list()]
    assert "cw/coding:medium" in ids


async def test_chat_completion(sdk: tuple[openai.AsyncOpenAI, FakeOllama, Vault]) -> None:
    client, fake, vault = sdk
    resp = await client.chat.completions.create(
        model="cw/coding:low", messages=[{"role": "user", "content": "hello"}], max_tokens=50
    )
    assert resp.choices[0].message.content == "Hi from Cairnward"
    assert resp.usage is not None and resp.usage.total_tokens == 17
    assert fake.requests[0]["options"]["num_predict"] == 50
    assert len(vault.list_conversations()) == 1


async def test_streaming(sdk: tuple[openai.AsyncOpenAI, FakeOllama, Vault]) -> None:
    client, _, _ = sdk
    stream = await client.chat.completions.create(
        model="cw/coding:low", messages=[{"role": "user", "content": "hello"}], stream=True
    )
    text = "".join([c.choices[0].delta.content or "" async for c in stream if c.choices])
    assert text == "Hi from Cairnward "


async def test_tool_calls(sdk: tuple[openai.AsyncOpenAI, FakeOllama, Vault]) -> None:
    client, fake, _ = sdk
    fake.tool_call = {"name": "get_weather", "arguments": {"city": "Kuala Lumpur"}}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    resp = await client.chat.completions.create(
        model="cw/coding:medium",
        messages=[{"role": "user", "content": "weather?"}],
        tools=tools,  # type: ignore[arg-type]
    )
    call = resp.choices[0].message.tool_calls[0]  # type: ignore[index]
    assert call.function.name == "get_weather" and '"Kuala Lumpur"' in call.function.arguments  # type: ignore[union-attr]
    assert resp.choices[0].finish_reason == "tool_calls"


async def test_errors_surface_as_sdk_exceptions(
    sdk: tuple[openai.AsyncOpenAI, FakeOllama, Vault],
) -> None:
    client, _, _ = sdk
    with pytest.raises(openai.NotFoundError):
        await client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "x"}]
        )
    bad = openai.AsyncOpenAI(
        base_url="http://127.0.0.1:8484/v1", api_key="cw-wrong", http_client=client._client
    )
    with pytest.raises(openai.AuthenticationError):
        await bad.models.list()
