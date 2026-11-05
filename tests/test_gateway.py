import json
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import Config, load_config
from cairnward.config.paths import Paths
from cairnward.gateway import ClientKeys, GatewayDeps, create_app, resolve
from cairnward.gateway.models import UnknownModel
from cairnward.gateway.recorder import prefix_hashes
from cairnward.policy import PolicyEngine
from cairnward.store import Vault
from tests.fakes.backends import FakeOllama, FakeOpenAI

BACKENDS = """
[backends.local-ollama]
driver = "ollama"
url = "http://127.0.0.1:11434"
[backends.llama]
driver = "openai_compat"
url = "http://127.0.0.1:8080/v1"
[backends.cloud]
driver = "openai_compat"
url = "https://openrouter.ai/api/v1"
location = "hosted"
[roles.main]
backend = "local-ollama"
model = "qwen3:30b"
[roles.fast]
backend = "local-ollama"
model = "qwen3:1.7b"
[roles.embed]
backend = "local-ollama"
model = "qwen3-embedding:0.6b"
"""


@dataclass
class Env:
    client: httpx.AsyncClient
    key: str
    cfg: Config
    vault: Vault
    audit: AuditLog
    ollama: FakeOllama
    llama: FakeOpenAI
    cloud: FakeOpenAI

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    async def chat(self, model: str, messages: list[dict[str, Any]], **kw: Any) -> httpx.Response:
        body = {"model": model, "messages": messages, **kw}
        return await self.client.post("/v1/chat/completions", headers=self.auth, json=body)


@pytest.fixture
async def gw(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    opened: list[Env] = []

    async def make(environments: dict[str, str] | None = None) -> Env:
        paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
        (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
        (paths.config_dir / "backends.toml").write_text(BACKENDS)
        for name, text in (environments or {}).items():
            (paths.config_dir / "environments" / f"{name}.toml").write_text(text)
        cfg = load_config(paths)
        fakes = {"local-ollama": FakeOllama(), "llama": FakeOpenAI(), "cloud": FakeOpenAI()}
        backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(
            fakes[n].handler))  # type: ignore[attr-defined]  # fmt: skip
        audit = AuditLog.open(paths)
        vault = Vault.open(paths)
        keys = ClientKeys.at(paths.state_dir)
        key = keys.add("open-webui")
        app = create_app(GatewayDeps(cfg, PolicyEngine(cfg, audit=audit), backends, keys, vault))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://127.0.0.1:8484")  # fmt: skip
        env = Env(client, key, cfg, vault, audit, fakes["local-ollama"],  # type: ignore[arg-type]
                  fakes["llama"], fakes["cloud"])  # type: ignore[arg-type]  # fmt: skip
        opened.append(env)
        return env

    yield make
    for e in opened:
        await e.client.aclose()
        e.vault.close()
        e.audit.close()


USER = [{"role": "user", "content": "What is 2+2?"}]


# ------------------------------------------------------------------ access


async def test_health_and_host_check(gw: Callable[..., Any]) -> None:
    g = await gw()
    assert (await g.client.get("/health")).json()["status"] == "ok"
    resp = await g.client.get("/health", headers={"Host": "evil.example"})
    assert resp.status_code == 421
    assert (await g.client.get("/health", headers={"Host": "[::1]:8484"})).status_code == 200


async def test_api_key_required(gw: Callable[..., Any]) -> None:
    g = await gw()
    assert (await g.client.get("/v1/models")).status_code == 401
    bad = await g.client.get("/v1/models", headers={"Authorization": "Bearer cw-wrong"})
    assert bad.status_code == 401
    resp = await g.client.post(
        "/v1/chat/completions", json={"model": "cw/coding:low", "messages": USER}
    )
    assert resp.status_code == 401 and g.ollama.requests == []


async def test_models_lists_virtual_models(gw: Callable[..., Any]) -> None:
    g = await gw()
    ids = [m["id"] for m in (await g.client.get("/v1/models", headers=g.auth)).json()["data"]]
    assert "cw/coding:low" in ids and "cw/scratch:high" in ids


# ------------------------------------------------------------------ routing and forwarding


async def test_virtual_model_sets_model_and_options(gw: Callable[..., Any]) -> None:
    g = await gw()
    resp = await g.chat("cw/coding:low", USER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "cw/coding:low"
    assert data["choices"][0]["message"]["content"] == "Hello from fake Ollama"
    sent = g.ollama.requests[0]
    assert sent["model"] == "qwen3:1.7b"  # low effort -> fast model
    assert sent["think"] is False and sent["options"]["num_ctx"] == 8192
    resp = await g.chat("cw/coding:high", USER)
    assert g.ollama.requests[1]["model"] == "qwen3:30b" and g.ollama.requests[1]["think"] is True


async def test_main_model_reasoning_stays_out_of_the_answer(gw: Callable[..., Any]) -> None:
    """Asked not to reason, the main model writes its reasoning into the answer, where a client
    takes it for the answer. Asked to, it comes back separately (real replies from qwen3:30b)."""
    g = await gw()
    g.ollama.reply, g.ollama.thinking = "The answer.", "We are given a question. Maybe [1]..."
    for effort in ("medium", "high"):
        resp = await g.chat(f"cw/coding:{effort}", USER)
        assert g.ollama.requests[-1]["model"] == "qwen3:30b"
        assert g.ollama.requests[-1]["think"] is True
        message = resp.json()["choices"][0]["message"]
        assert message["content"] == "The answer."
        assert "Maybe" in message["reasoning_content"]


async def test_messages_forwarded_unchanged(gw: Callable[..., Any]) -> None:
    """Prefix stability (FR-GW-7): the backend sees exactly the client's messages."""
    g = await gw({"fastenv": 'backend = "llama"\n'})
    history = [
        {"role": "system", "content": "Be brief.", "name": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "Hi ✓"}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]  # fmt: skip
    resp = await g.chat("cw/fastenv:medium", history, stop=["\n\n"], seed=3, unknown_field=1)
    assert resp.status_code == 200
    sent = g.llama.requests[0]
    assert json.dumps(sent["messages"]) == json.dumps(history)
    assert sent["stop"] == ["\n\n"] and sent["seed"] == 3 and "unknown_field" not in sent


async def test_unknown_model(gw: Callable[..., Any]) -> None:
    g = await gw()
    for name in ("gpt-4", "cw/nope:low", "cw/coding:turbo", "cw/raw/"):
        resp = await g.chat(name, USER)
        assert resp.status_code == 404 and resp.json()["error"]["code"] == "model_not_found"


def test_resolve_raw_and_defaults(tmp_path: Path) -> None:
    cfg = load_config(Paths(tmp_path, tmp_path / "d", tmp_path / "s"))
    r = resolve(cfg, "cw/raw/qwen3:8b")
    assert (r.environment, r.model, r.backend) == ("coding", "qwen3:8b", "local-ollama")
    assert resolve(cfg, "cw/scratch").effort == "low"  # environment default
    with pytest.raises(UnknownModel):
        resolve(cfg, "qwen3:30b")


# ------------------------------------------------------------------ policy and audit


async def test_policy_denies_hosted_backend(gw: Callable[..., Any]) -> None:
    g = await gw({"cloudy": 'backend = "cloud"\n[privacy]\npreset = "strict"\n'})
    resp = await g.chat("cw/cloudy:low", USER)
    assert resp.status_code == 403 and resp.json()["error"]["type"] == "policy_denied"
    assert "hosted_backends = deny" in resp.json()["error"]["message"]
    assert g.cloud.requests == []
    (rec,) = g.audit.records()
    assert (rec.action, rec.decision, rec.client) == ("model.call", "deny", "open-webui")


async def test_audit_pre_and_post(gw: Callable[..., Any]) -> None:
    g = await gw()
    await g.chat("cw/coding:low", USER)
    pre, post = g.audit.records()
    assert pre.actor == "client:open-webui" and pre.details["virtual_model"] == "cw/coding:low"
    assert pre.params_digest and post.result_digest
    assert post.details["prompt_tokens"] == 12 and post.details["completion_tokens"] == 5
    assert post.conversation_id is None and post.details["conversation"]
    assert g.audit.verify().ok


async def test_backend_failure_is_502_and_audited(gw: Callable[..., Any]) -> None:
    g = await gw()
    g.ollama.fail_status = 500
    resp = await g.chat("cw/coding:low", USER)
    assert resp.status_code == 502
    post = g.audit.records()[-1]
    assert post.outcome == "error" and "500" in post.details["error"]


# ------------------------------------------------------------------ vault


async def test_conversation_continues_across_turns(gw: Callable[..., Any]) -> None:
    g = await gw()
    r1 = (await g.chat("cw/coding:low", USER)).json()
    reply = r1["choices"][0]["message"]
    turn2 = [*USER, {"role": "assistant", "content": reply["content"]},  # client drops extras
             {"role": "user", "content": "And 3+3?"}]  # fmt: skip
    await g.chat("cw/coding:low", turn2)
    (conv,) = g.vault.list_conversations()
    assert (
        conv.title == "What is 2+2?"
        and conv.environment == "coding"
        and conv.client == "open-webui"
    )
    msgs = g.vault.messages(conv.id)
    assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert all(b.parent_id == a.id for a, b in pairwise(msgs))
    assert msgs[1].model == "qwen3:1.7b" and msgs[1].effort == "low"


async def test_edited_history_branches_in_same_conversation(gw: Callable[..., Any]) -> None:
    g = await gw()
    r1 = (await g.chat("cw/coding:low", USER)).json()
    a1 = {"role": "assistant", "content": r1["choices"][0]["message"]["content"]}
    await g.chat("cw/coding:low", [*USER, a1, {"role": "user", "content": "And 3+3?"}])
    await g.chat("cw/coding:low", [*USER, a1, {"role": "user", "content": "And 4+4?"}])
    (conv,) = g.vault.list_conversations()
    msgs = g.vault.messages(conv.id)
    assert len(msgs) == 6
    branch_points = [m for m in msgs if m.role == "user" and m.parent_id == msgs[1].id]
    assert len(branch_points) == 2  # both follow-ups hang off the first reply


async def test_vault_off_stores_nothing(gw: Callable[..., Any]) -> None:
    g = await gw()
    await g.chat("cw/scratch:low", USER)  # scratch = Open preset, vault off
    assert g.vault.list_conversations() == []
    assert g.audit.records()[-1].details["vault"] == "off"


async def test_crypto_shred_setting_respected(gw: Callable[..., Any]) -> None:
    g = await gw({"plain": '[privacy]\ncrypto_shred = "off"\n'})
    await g.chat("cw/plain:low", USER)
    (conv,) = g.vault.list_conversations()
    assert conv.encrypted is False


def test_prefix_hashes_ignore_echo_differences() -> None:
    a = [{"role": "assistant", "content": "x", "reasoning_content": "thinking..."}]
    b = [{"role": "assistant", "content": "x"}]
    assert prefix_hashes(a) == prefix_hashes(b)
    assert prefix_hashes([{"role": "assistant", "content": None}]) == prefix_hashes(
        [{"role": "assistant", "content": ""}])  # fmt: skip


# ------------------------------------------------------------------ streaming


async def test_streaming(gw: Callable[..., Any]) -> None:
    g = await gw()
    g.ollama.reply = "four is the answer"
    async with g.client.stream("POST", "/v1/chat/completions", headers=g.auth,
                               json={"model": "cw/coding:low", "messages": USER,
                                     "stream": True}) as resp:  # fmt: skip
        assert resp.status_code == 200
        lines = [line async for line in resp.aiter_lines() if line.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[5:]) for line in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "four is the answer "
    assert all(c["model"] == "cw/coding:low" for c in chunks)
    (conv,) = g.vault.list_conversations()
    assert g.vault.messages(conv.id)[-1].body["content"] == "four is the answer "
    post = g.audit.records()[-1]
    assert post.outcome == "ok" and post.details["completion_tokens"] == 5


async def test_streaming_tool_calls_recorded(gw: Callable[..., Any]) -> None:
    g = await gw()
    g.ollama.tool_call = {"name": "fs.read", "arguments": {"path": "a.py"}}
    async with g.client.stream("POST", "/v1/chat/completions", headers=g.auth,
                               json={"model": "cw/coding:low", "messages": USER,
                                     "stream": True}) as resp:  # fmt: skip
        _ = [line async for line in resp.aiter_lines()]
    (conv,) = g.vault.list_conversations()
    reply = g.vault.messages(conv.id)[-1].body
    assert reply["tool_calls"][0]["function"] == {
        "name": "fs.read",
        "arguments": '{"path": "a.py"}',
    }


# ------------------------------------------------------------------ client keys


def test_client_keys(tmp_path: Path) -> None:
    keys = ClientKeys.at(tmp_path)
    key = keys.add("aider")
    assert key.startswith("cw-") and keys.identify(key) == "aider"
    assert key not in keys.path.read_text()  # only the hash is stored
    if sys.platform != "win32":
        assert keys.path.stat().st_mode & 0o777 == 0o600
    new = keys.add("aider")  # rotate
    assert keys.identify(key) is None and keys.identify(new) == "aider"
    assert list(keys.names()) == ["aider"]
    assert keys.remove("aider") and keys.identify(new) is None
    assert keys.identify(None) is None and keys.identify("sk-other") is None


def test_committed_openapi_is_up_to_date() -> None:
    """Contract test (FR-API-4): run `cairnward api schema` after changing the gateway API."""
    from cairnward.cli import _openapi_text

    committed = Path(__file__).resolve().parents[1] / "api" / "openapi.json"
    assert committed.read_text(encoding="utf-8") == _openapi_text()


async def test_embeddings(gw: Callable[..., Any]) -> None:
    g = await gw()
    BACK = g.cfg.backends
    assert "embed" in BACK.roles
    resp = await g.client.post(
        "/v1/embeddings", headers=g.auth, json={"model": "cw/coding:embed", "input": ["a", "bb"]}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [d["index"] for d in data["data"]] == [0, 1] and len(data["data"][0]["embedding"]) == 3
    assert g.ollama.requests[-1] == {"model": "qwen3-embedding:0.6b", "input": ["a", "bb"]}
    pre, post = g.audit.records()
    assert pre.details["operation"] == "embeddings" and post.details["prompt_tokens"] == 4
    assert g.vault.list_conversations() == []  # embeddings aren't conversations
    bad = await g.client.post(
        "/v1/embeddings", headers=g.auth, json={"model": "cw/coding:low", "input": "x"}
    )
    assert bad.status_code == 404
    ids = [m["id"] for m in (await g.client.get("/v1/models", headers=g.auth)).json()["data"]]
    assert "cw/coding:embed" in ids


LEAK = "We are given a question. Maybe 3? No, 4.\n</think>\n\nThe answer is 4."


async def test_reasoning_written_into_the_answer_is_separated(gw: Any) -> None:
    """T-26, in the shape of a real reply (qwen3:30b with think=false): a client gets only the
    answer, the audit log notes it, and the model is asked for reasoning from then on."""
    g = await gw({"quiet": '[privacy]\nvault = "full"\n'})
    g.ollama.reply = LEAK
    first = (await g.chat("cw/coding:low", USER)).json()["choices"][0]["message"]
    assert first["content"] == "The answer is 4."
    assert first["reasoning_content"] == "We are given a question. Maybe 3? No, 4."
    assert g.ollama.requests[-1]["think"] is False
    post = [r for r in g.audit.records() if r.action == "model.call" and r.phase == "post"][-1]
    assert post.details["reasoning_leak"] is True
    g.ollama.reply = "The answer is 4."
    await g.chat("cw/coding:low", USER)
    assert g.ollama.requests[-1]["think"] is True  # remembered, so it comes back separately


async def test_streamed_leak_is_remembered_for_the_next_call(gw: Any) -> None:
    g = await gw()
    g.ollama.reply = LEAK
    resp = await g.chat("cw/coding:low", USER, stream=True)
    assert resp.status_code == 200 and "4." in resp.text  # a stream can't be taken back
    assert g.ollama.requests[-1]["think"] is False
    conv = g.vault.list_conversations()[0]
    stored = g.vault.messages(conv.id)[-1].body
    assert stored["content"] == "The answer is 4." and "Maybe 3" in stored["reasoning_content"]
    await g.chat("cw/coding:low", USER)
    assert g.ollama.requests[-1]["think"] is True


async def test_a_normal_answer_is_left_alone(gw: Any) -> None:
    g = await gw()
    g.ollama.reply = "The answer is 4."
    message = (await g.chat("cw/coding:low", USER)).json()["choices"][0]["message"]
    assert message["content"] == "The answer is 4." and "reasoning_content" not in message
    await g.chat("cw/coding:low", USER)
    assert g.ollama.requests[-1]["think"] is False
