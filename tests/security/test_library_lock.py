"""Locking a literature library (FR-ENC-1, T-24): real SQLite, sqlite-vec, FTS5 and age."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward import vault_cli
from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.cli import main
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.policy import PolicyEngine
from cairnward.research import InProcessEmbedder, Research
from cairnward.research.grobid import meta_dir
from cairnward.research.library import LibraryError, LibraryLockedError
from cairnward.research.lock import item_id, lock_library, unlock_library
from cairnward.store import Vault
from cairnward.store.vault import LockedError, VaultError
from tests.fakes.backends import FakeOllama
from tests.fakes.pdf import make_pdf

SECRET = "correct horse battery staple 42"
MARKER = "xylophonicmarker"  # one token, so it would also show up in the keyword index
TITLE = "Zanthoxylum confidential title"
QUERY = f"{MARKER} sleep"


def pdf(text: str, title: str) -> bytes:
    return make_pdf(
        [[f"Line {i}: {text} improved working memory." for i in range(30)]], title=title
    )


@pytest.fixture
async def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("CAIRNWARD_CACHE_DIR", str(tmp_path / "cache"))
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    fake = FakeOllama()
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fake.handler))
    audit = AuditLog.open(paths)
    engine = PolicyEngine(cfg, audit=audit)
    embedder = InProcessEmbedder(cfg, engine, backends, environment="litreview-offline")
    for name, text, title in (("thesis", MARKER, TITLE), ("other", "ordinary", "Other paper")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "a.pdf").write_bytes(pdf(text, title))
        with Research.open(paths) as r:
            r.create(name, folder, "litreview-offline")
            await r.index(name, embedder)
    yield paths, embedder
    audit.close()
    await backends.aclose()


def raw_bytes(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in folder.rglob("*") if p.is_file())


async def test_locking_leaves_no_text_anywhere_in_the_data_folder(env: Any) -> None:
    paths, embedder = env
    with Research.open(paths) as r:
        assert await r.search("thesis", QUERY, embedder, k=2)
    assert MARKER.encode() in raw_bytes(paths.data_dir)  # so the check below means something
    with Research.open(paths) as r, Vault.open(paths) as v:
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
    data = raw_bytes(paths.data_dir)
    for secret in (MARKER, MARKER[:8], "Zanthoxylum", "confidential"):
        assert secret.encode() not in data, secret


async def test_a_locked_library_refuses_everything_that_reads_it(env: Any) -> None:
    paths, embedder = env
    with Research.open(paths) as r, Vault.open(paths) as v:
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
        for call in (lambda: r.documents("thesis"), lambda: r.chunk_rows("thesis"),
                     lambda: r.get("thesis")):  # fmt: skip
            with pytest.raises(LibraryLockedError, match="cairnward library unlock"):
                call()
        with pytest.raises(LibraryLockedError):
            await r.search("thesis", QUERY, embedder)
        with pytest.raises(LibraryLockedError):
            await r.index("thesis", embedder)
        listed = {lib.name: (lib.locked, docs) for lib, docs, _ in r.list_libraries()}
        assert listed["thesis"] == (True, 0) and listed["other"] == (False, 1)
        assert r.documents("other") and await r.search("other", "ordinary", embedder, k=2)


async def test_unlocking_restores_search_exactly(env: Any) -> None:
    paths, embedder = env
    with Research.open(paths) as r, Vault.open(paths) as v:
        before = await r.search("thesis", QUERY, embedder, k=3)
        titles = [d.title for d in r.documents("thesis")]
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
        with pytest.raises(LockedError, match="wrong passphrase"):
            unlock_library(v, r, paths.data_dir, "thesis", "not the passphrase")
        assert r.find("thesis").locked
        unlock_library(v, r, paths.data_dir, "thesis", SECRET)
        assert not r.find("thesis").locked and not r.locked_path("thesis").exists()
        assert not v.is_locked(item_id("thesis"))
        after = await r.search("thesis", QUERY, embedder, k=3)
        assert [h.text for h in after] == [h.text for h in before]
        assert [(h.vector_rank is not None, h.keyword_rank is not None) for h in after] == [
            (h.vector_rank is not None, h.keyword_rank is not None) for h in before
        ]
        assert [d.title for d in r.documents("thesis")] == titles


async def test_ids_taken_while_locked_dont_collide(env: Any, tmp_path: Path) -> None:
    paths, embedder = env
    with Research.open(paths) as r, Vault.open(paths) as v:
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
        folder = tmp_path / "third"
        folder.mkdir()
        (folder / "c.pdf").write_bytes(pdf("thirdpaper", "Third"))
        r.create("third", folder, "litreview-offline")
        await r.index("third", embedder)  # reuses the freed document and chunk ids
        unlock_library(v, r, paths.data_dir, "thesis", SECRET)
        assert (await r.search("third", "thirdpaper", embedder, k=1))[0].path == "c.pdf"
        assert MARKER in (await r.search("thesis", MARKER, embedder, k=1))[0].text


async def test_grobid_metadata_and_paperqa_cache_dont_stay_readable(env: Any) -> None:
    from cairnward.research.lock import _paperqa_cache

    paths, _ = env
    meta = meta_dir(paths.data_dir, "thesis")
    meta.mkdir(parents=True)
    (meta / "a.pdf.tei.xml").write_text(f"<tei>{MARKER}</tei>")
    cache = _paperqa_cache("thesis")
    cache.mkdir(parents=True)
    (cache / "index.pkl").write_text(MARKER)
    with Research.open(paths) as r, Vault.open(paths) as v:
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
        assert not meta.exists() and not cache.exists()
        unlock_library(v, r, paths.data_dir, "thesis", SECRET)
    assert (meta / "a.pdf.tei.xml").read_text() == f"<tei>{MARKER}</tei>"


async def test_recovery_key_opens_it(env: Any) -> None:
    paths, embedder = env
    with Research.open(paths) as r, Vault.open(paths) as v:
        recovery = v.create_recovery_key()
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
        v.recover(item_id("thesis"), recovery, "a brand new passphrase")
        unlock_library(v, r, paths.data_dir, "thesis", "a brand new passphrase")
        assert await r.search("thesis", MARKER, embedder, k=1)


async def test_crash_after_sealing_is_finished_by_locking_again(env: Any) -> None:
    paths, embedder = env
    with Research.open(paths) as r, Vault.open(paths) as v:
        original = r.wipe_rows

        def crash(_lib: Any) -> None:
            raise OSError("power cut")

        r.wipe_rows = crash  # type: ignore[method-assign,assignment]
        with pytest.raises(OSError, match="power cut"):
            lock_library(v, r, paths.data_dir, "thesis", SECRET)
        r.wipe_rows = original  # type: ignore[method-assign]
        with pytest.raises(LibraryLockedError):  # nothing can read it, though the rows remain
            r.get("thesis")
        lock_library(v, r, paths.data_dir, "thesis", "any passphrase, the first one counts")
        assert MARKER.encode() not in raw_bytes(paths.data_dir)
        unlock_library(v, r, paths.data_dir, "thesis", SECRET)
        assert await r.search("thesis", MARKER, embedder, k=1)


async def test_crash_after_saving_the_key_doesnt_leave_it_stuck(env: Any) -> None:
    paths, embedder = env
    with Research.open(paths) as r, Vault.open(paths) as v:
        v.lock_item(item_id("thesis"), SECRET)  # the crash came right after this
        assert await r.search("thesis", MARKER, embedder, k=1)  # not sealed, so still readable
        with pytest.raises(LockedError, match="wrong passphrase"):
            lock_library(v, r, paths.data_dir, "thesis", "a different passphrase")
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
        assert r.find("thesis").locked and MARKER.encode() not in raw_bytes(paths.data_dir)
        unlock_library(v, r, paths.data_dir, "thesis", SECRET)


async def test_crash_while_unlocking_keeps_the_lock_and_can_be_redone(env: Any) -> None:
    paths, embedder = env
    with Research.open(paths) as r, Vault.open(paths) as v:
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
        # the rows went back, but the crash came before the file and key were removed
        data = __import__("json").loads(
            __import__("cairnward.store.crypto", fromlist=["x"]).open_sealed(
                v.open_item(item_id("thesis"), SECRET), r.locked_path("thesis").read_bytes()
            )
        )
        r.import_rows(r.find("thesis"), data)
        unlock_library(v, r, paths.data_dir, "thesis", SECRET)  # replaces, doesn't duplicate
        assert len(r.documents("thesis")) == 1
        assert await r.search("thesis", MARKER, embedder, k=1)


async def test_double_lock_and_unknown_library(env: Any) -> None:
    paths, _ = env
    with Research.open(paths) as r, Vault.open(paths) as v:
        with pytest.raises(LibraryError, match="no library"):
            lock_library(v, r, paths.data_dir, "nope", SECRET)
        with pytest.raises(VaultError, match="at least 8"):
            lock_library(v, r, paths.data_dir, "thesis", "short")
        assert not r.find("thesis").locked  # a refused passphrase changed nothing
        assert r.documents("thesis")


async def test_cli_and_models_dont_see_a_locked_library(
    env: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, _ = env
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    monkeypatch.setattr(vault_cli, "ask_passphrase", lambda _prompt: SECRET)
    base = ["--config-dir", str(paths.config_dir), "library"]
    assert main([*base, "lock", "thesis"]) == 0
    out = capsys.readouterr().out
    assert "aren't encrypted" in out and SECRET not in out
    assert main([*base, "list"]) == 0
    out = capsys.readouterr().out
    assert "thesis" in out and "locked" in out and TITLE not in out
    assert main([*base, "show", "thesis"]) != 0
    capsys.readouterr()
    with AuditLog.open(paths) as log:
        rec = [x for x in log.records() if x.action == "library.lock"]
    assert rec and rec[0].target == "thesis" and SECRET not in rec[0].to_json_line()

    from cairnward.mcp.hub import McpHub

    cfg = load_config(paths)
    hub: Any = McpHub.__new__(McpHub)
    hub.cfg = cfg
    assert hub._libraries("litreview-offline") == ["other"]

    assert main([*base, "unlock", "thesis"]) == 0
    assert hub._libraries("litreview-offline") == ["other", "thesis"]


async def test_removing_a_locked_library_destroys_its_key(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = env
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    with Research.open(paths) as r, Vault.open(paths) as v:
        lock_library(v, r, paths.data_dir, "thesis", SECRET)
        sealed = r.locked_path("thesis")
    assert main(["--config-dir", str(paths.config_dir), "library", "remove", "thesis"]) == 0
    assert not sealed.exists()
    with Research.open(paths) as r, Vault.open(paths) as v:
        assert not v.is_locked(item_id("thesis"))
        with pytest.raises(LibraryError):
            r.find("thesis")
