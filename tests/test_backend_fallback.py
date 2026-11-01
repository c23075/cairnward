"""Fallback only to local backends, logged (FR-BACK-3)."""

import json
from pathlib import Path
from typing import Any

import httpx

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.gateway import ClientKeys, GatewayDeps, create_app
from cairnward.policy import PolicyEngine
from cairnward.store import Vault
from tests.fakes.backends import FakeOllama, FakeOpenAI
from tests.test_gateway import BACKENDS

FALLBACK = BACKENDS.replace(
    '[backends.llama]\ndriver = "openai_compat"\n',
    '[backends.llama]\ndriver = "openai_compat"\nfallback = "local-ollama"\n'
    'fallback_model = "qwen3:1.7b"\n',
)
USER = [{"role": "user", "content": "hi"}]


async def setup(
    tmp_path: Path, status: int
) -> tuple[httpx.AsyncClient, FakeOllama, FakeOpenAI, AuditLog, Vault]:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "backends.toml").write_text(FALLBACK)
    (paths.config_dir / "environments" / "e.toml").write_text('backend = "llama"\n')
    cfg = load_config(paths)
    ollama, llama = FakeOllama(reply="from the fallback"), FakeOpenAI(fail_status=status)
    fakes: dict[str, Any] = {"local-ollama": ollama, "llama": llama, "cloud": FakeOpenAI()}
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fakes[n].handler))
    audit, vault = AuditLog.open(paths), Vault.open(paths)
    keys = ClientKeys.at(paths.state_dir)
    key = keys.add("jan")
    app = create_app(GatewayDeps(cfg, PolicyEngine(cfg, audit=audit), backends, keys, vault))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://127.0.0.1:8484",
                               headers={"Authorization": f"Bearer {key}"})  # fmt: skip
    return client, ollama, llama, audit, vault


async def test_falls_back_on_server_errors(tmp_path: Path) -> None:
    client, ollama, llama, audit, vault = await setup(tmp_path, 503)
    try:
        resp = await client.post(
            "/v1/chat/completions", json={"model": "cw/e:low", "messages": USER}
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "from the fallback"
        assert "answered by the local fallback local-ollama" in resp.headers["X-Cairnward-Warning"]
        assert ollama.requests[-1]["model"] == "qwen3:1.7b" and llama.requests
        # streaming: falls back too when nothing was sent yet
        async with client.stream("POST", "/v1/chat/completions", json={
                "model": "cw/e:low", "messages": USER, "stream": True}) as s:  # fmt: skip
            body = (await s.aread()).decode()
        assert "fallback" in body and "[DONE]" in body
    finally:
        await client.aclose()
    recs = audit.records()
    fb = [r for r in recs if r.action == "model.fallback"]
    assert len(fb) == 2 and fb[0].details["from"] == "llama" and fb[0].target == "local-ollama"
    post = next(r for r in recs if r.action == "model.call" and r.phase == "post")
    assert post.details["fallback"]["to"] == "local-ollama"
    audit.close()
    vault.close()


async def test_client_errors_are_not_retried(tmp_path: Path) -> None:
    client, ollama, _, audit, vault = await setup(tmp_path, 400)
    try:
        resp = await client.post(
            "/v1/chat/completions", json={"model": "cw/e:low", "messages": USER}
        )
        assert resp.status_code == 400 and ollama.requests == []
        assert not any(r.action == "model.fallback" for r in audit.records())
        assert json.loads(resp.text)["error"]["type"] == "backend_error"
    finally:
        await client.aclose()
        audit.close()
        vault.close()
