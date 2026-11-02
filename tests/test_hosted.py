"""Hosted providers (FR-BACK-4/5, D70): templates that load as valid backends; backends that
may train on prompts are answered only in scratch environments (unless training_backends
= allow, a visible weakening); hosted models are marked ☁."""

from pathlib import Path

import httpx
import pytest

from cairnward.backends.providers import PROVIDERS, template
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.policy import PolicyEngine
from cairnward.policy.engine import Action


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (p.config_dir / "environments").mkdir(parents=True)
    blocks = "\n".join(template(name) for name in PROVIDERS)
    (p.config_dir / "backends.toml").write_text(
        blocks + '\n[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:11434"\n'
    )
    for name, extra in (("work", ""), ("play", "scratch = true\n")):
        (p.config_dir / "environments" / f"{name}.toml").write_text(
            f'{extra}backend = "openrouter-free"\nmodels = {{ main = "x" }}\n'
            '[privacy]\nhosted_backends = "allow"\nnetwork = "open"\n'
        )
    return p


def test_templates_are_valid_backends(paths: Paths) -> None:
    cfg = load_config(paths)
    for name, p in PROVIDERS.items():
        b = cfg.backends.backends[name]
        assert b.location == "hosted" and b.api_key == f"env:{p.key_env}"
        assert b.trains_on_prompts is p.trains_on_prompts
    assert cfg.backends.backends["gemini-free"].trains_on_prompts
    with pytest.raises(KeyError, match="unknown provider"):
        template("chatgpt")


def rule(cfg: object, backend: str, env: str) -> str:
    engine = PolicyEngine(cfg)  # type: ignore[arg-type]
    r = engine.rule_for(Action("model.call", backend, environment=env))
    return f"{r.base}: {r.reason}"


def test_training_backends_only_in_scratch(paths: Paths) -> None:
    cfg = load_config(paths)
    assert rule(cfg, "openrouter-free", "work").startswith("deny: 'openrouter-free' may train")
    assert rule(cfg, "openrouter-free", "play") == "allow: hosted_backends = allow"
    assert rule(cfg, "openrouter", "work") == "allow: hosted_backends = allow"  # paid: no mark
    assert rule(cfg, "openrouter-free", "scratch").startswith("allow")  # built-in scratch env
    # switching the restriction off (a weakening, shown and audited like the others)
    (paths.config_dir / "cairnward.toml").write_text('[privacy]\ntraining_backends = "allow"\n')
    cfg = load_config(paths)
    assert rule(cfg, "openrouter-free", "work").startswith("allow")
    assert "training_backends" in cfg.privacy("work").weaker_than_strict


async def test_hosted_models_are_marked(paths: Paths) -> None:
    from cairnward.backends import Backends
    from cairnward.gateway import ClientKeys, GatewayDeps, create_app
    from cairnward.store import Vault

    cfg = load_config(paths)
    keys = ClientKeys.at(paths.state_dir)
    key = keys.add("t")
    with Vault.open(paths) as vault:
        app = create_app(GatewayDeps(cfg, PolicyEngine(cfg), Backends(cfg.backends), keys, vault))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8484") as c:  # fmt: skip
            models = (await c.get("/v1/models", headers={"Authorization": f"Bearer {key}"})).json()
    owners = {m["id"]: m["owned_by"] for m in models["data"]}
    assert owners["cw/work:medium"] == "cairnward ☁ hosted (may train on prompts)"
    assert owners["cw/coding:medium"] == "cairnward"
