"""Conversation locks (FR-ENC-1) and passphrase hygiene (FR-ENC-2)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cairnward import vault_cli
from cairnward.cli import main
from cairnward.config.paths import Paths
from cairnward.store import Vault
from cairnward.store.vault import LockedError, VaultError
from tests.test_gateway import gw  # noqa: F401  (fixture)

SECRET = "correct horse battery staple 42"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def test_lock_unlock_cycle(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid = v.create_conversation(environment="coding", title="Private notes")
        v.add_message(cid, {"role": "user", "content": "my diary"})
        with pytest.raises(VaultError, match="at least 8"):
            v.lock(cid, "short")
        v.lock(cid, SECRET)
        assert v.is_locked(cid) and not v.readable(cid)
        conv = v.conversation(cid)
        assert conv.locked and conv.title is None
        with pytest.raises(LockedError):
            v.messages(cid)
        with pytest.raises(LockedError):
            v.add_message(cid, {"role": "user", "content": "more"})
        with pytest.raises(LockedError, match="wrong passphrase"):
            v.unlock(cid, "not the passphrase")
        v.unlock(cid, SECRET, minutes=1)
        assert v.messages(cid)[0].body["content"] == "my diary"
        v.relock(cid)
        assert not v.readable(cid)
    with Vault.open(paths) as v:  # unlocking is per process, in memory only
        assert not v.readable(cid)
        v.unlock(cid, SECRET, permanently=True)
        assert not v.is_locked(cid) and v.conversation(cid).title == "Private notes"


def test_unencrypted_conversations_cant_be_locked(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid = v.create_conversation(crypto_shred=False)
        with pytest.raises(VaultError, match="without encryption"):
            v.lock(cid, SECRET)


def test_cli_and_no_passphrase_leaks(paths: Paths, monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    paths.config_dir.mkdir(parents=True)
    with Vault.open(paths) as v:
        cid = v.create_conversation(environment="coding", title="Medical")
        v.add_message(cid, {"role": "user", "content": "results"})
    answers = iter([SECRET, SECRET, SECRET, SECRET])
    monkeypatch.setattr(vault_cli, "ask_passphrase", lambda prompt: next(answers))
    base = ["--config-dir", str(paths.config_dir), "vault"]
    assert main([*base, "lock", cid[:8]]) == 0
    assert main([*base, "list"]) == 0
    out = capsys.readouterr().out
    assert "--L" in out and "(locked)" in out and "Medical" not in out
    assert main([*base, "search", "results"]) == 0
    assert "no matches" in capsys.readouterr().out  # locked: not searched
    assert main([*base, "show", cid[:8]]) == 0  # asks for the passphrase
    assert "results" in capsys.readouterr().out
    assert main([*base, "unlock", cid[:8]]) == 1  # no server running
    assert "isn't running" in capsys.readouterr().out
    # FR-ENC-2: the passphrase is nowhere on disk
    for f in [*paths.data_dir.rglob("*"), *paths.state_dir.rglob("*")]:
        if f.is_file():
            assert SECRET.encode() not in f.read_bytes(), f


LOCAL = [{"role": "user", "content": "a locked chat"}]


async def test_gateway_rules(gw: Callable[..., Any]) -> None:  # noqa: F811
    g = await gw({"cloudy": 'backend = "cloud"\n[privacy]\nhosted_backends = "allow"\n'
                            'network = "open"\nredaction = "off"\n'})  # fmt: skip
    first = await g.chat("cw/coding:low", LOCAL)
    assert first.status_code == 200
    reply = first.json()["choices"][0]["message"]
    cid = g.vault.list_conversations()[0].id
    g.vault.lock(cid, SECRET)
    follow = [*LOCAL, reply, {"role": "user", "content": "and more"}]
    # never to hosted backends, even when unlocked
    g.vault.unlock(cid, SECRET, minutes=1)
    resp = await g.chat("cw/cloudy:low", follow)
    assert resp.status_code == 403 and resp.json()["error"]["type"] == "locked_conversation"
    assert g.cloud.requests == []
    # local model, unlocked: answered and stored
    assert (await g.chat("cw/coding:low", follow)).status_code == 200
    assert len(g.vault.messages(cid)) == 4
    # local model, locked: answered, not stored, and the client is told
    g.vault.relock(cid)
    again = [*follow, {"role": "assistant", "content": "x"}, {"role": "user", "content": "?"}]
    resp = await g.chat("cw/coding:low", again)
    assert resp.status_code == 200 and "locked in the vault" in resp.headers["X-Cairnward-Warning"]
    post = [r for r in g.audit.records() if r.action == "model.call" and r.phase == "post"][-1]
    assert post.details["vault_note"] == "locked: not stored"


async def test_server_unlock_endpoint(paths: Paths) -> None:
    import httpx

    from cairnward.audit import AuditLog
    from cairnward.config import load_config
    from cairnward.core.management import management_app
    from cairnward.gateway.clients import RunKeys

    cfg = load_config(paths)
    with Vault.open(paths) as v, AuditLog.open(paths) as audit:
        cid = v.create_conversation(title="t")
        v.lock(cid, SECRET)
        app = management_app(cfg, audit=audit, run_keys=RunKeys(), egress=None, isolated=False,
                             vault=v)  # fmt: skip
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cairnward") as c:  # fmt: skip
            bad = await c.post(f"/v1/vault/{cid}/unlock", json={"passphrase": "nope"})
            assert bad.status_code == 400 and "wrong passphrase" in bad.text
            ok = await c.post(f"/v1/vault/{cid}/unlock", json={"passphrase": SECRET, "minutes": 2})
            assert ok.status_code == 200 and v.readable(cid)
            await c.post("/v1/vault/relock", json={})
            assert not v.readable(cid)
        rec = audit.records()[-1]
        assert rec.action == "vault.unlock" and SECRET not in str(rec.details)
