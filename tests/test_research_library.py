import hashlib
import json
import math
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.policy import PolicyEngine
from cairnward.research import (
    EmbeddingDenied,
    InProcessEmbedder,
    LibraryError,
    PdfError,
    Research,
    chunk_pages,
    read_pdf,
)
from cairnward.research.chunking import CHARS_PER_TOKEN
from cairnward.research.library import fts_query
from cairnward.store.db import METADATA, unmanaged_table
from tests.fakes.backends import FakeOllama
from tests.fakes.pdf import HashEmbedder, make_pdf

SENT = "Participants who slept longer showed better working memory in the follow-up test."


def paper(topic: str, n_pages: int = 3, lines: int = 30) -> list[list[str]]:
    return [
        [f"{topic} page {p} line {i}: {SENT}" for i in range(lines)] for p in range(1, n_pages + 1)
    ]


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


@pytest.fixture
def research(paths: Paths) -> Iterator[Research]:
    with Research.open(paths) as r:
        yield r


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    f = tmp_path / "papers"
    f.mkdir()
    (f / "sleep.pdf").write_bytes(make_pdf(paper("Sleep and memory"), title="Sleep and Memory"))
    (f / "sub").mkdir()
    (f / "sub" / "caffeine.pdf").write_bytes(make_pdf(
        [[f"Caffeine reaction time trial {i}: caffeine improved reaction time in shift workers "
          "(CYP1A2 genotype)." for i in range(30)]] * 2, title="Caffeine Trial"))  # fmt: skip
    return f


# ------------------------------------------------------------------ chunking


def test_chunks_respect_size_and_overlap() -> None:
    pages = ["\n\n".join(f"Paragraph {i}. {SENT} {SENT}" for i in range(40))] * 3
    chunks = chunk_pages(pages, target_tokens=200, overlap_tokens=40)
    assert len(chunks) > 5
    assert all(c.tokens <= 200 + 40 for c in chunks)
    for a, b in pairwise(chunks):
        assert b.text[:60] in a.text or a.text[-60:] in b.text  # overlap carried over
    assert chunks[0].page_start == 1 and chunks[-1].page_end == 3
    assert [c.seq for c in chunks] == list(range(len(chunks)))


def test_chunks_track_pages_and_keep_small_tail() -> None:
    chunks = chunk_pages(["A" * 50 + ".", "", "Short tail on page three."], target_tokens=100,
                         overlap_tokens=10)  # fmt: skip
    assert chunks[-1].page_end == 3 and "Short tail" in chunks[-1].text


def test_huge_sentence_is_split() -> None:
    word = "lorem "
    chunks = chunk_pages([word * 2000], target_tokens=100, overlap_tokens=10)
    assert all(len(c.text) <= 100 * CHARS_PER_TOKEN + 10 for c in chunks)
    assert sum(c.text.count("lorem") for c in chunks) >= 2000


def test_empty_and_bad_settings() -> None:
    assert chunk_pages(["", "  "]) == []
    with pytest.raises(ValueError):
        chunk_pages(["x"], target_tokens=50, overlap_tokens=50)


# ------------------------------------------------------------------ PDFs


def test_read_pdf(tmp_path: Path) -> None:
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf([["First page about hypo-", "thesis testing."], ["Second page."]],
                           title="My Title"))  # fmt: skip
    t = read_pdf(p)
    assert t.title == "My Title" and t.year == 2024 and len(t.pages) == 2
    assert "hypothesis testing" in t.pages[0] and t.pages[1] == "Second page."


def test_broken_pdf_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4 not really a pdf")
    with pytest.raises(PdfError):
        read_pdf(p)


# ------------------------------------------------------------------ libraries


def test_create_validation(research: Research, folder: Path, tmp_path: Path) -> None:
    research.create("thesis", folder, "litreview-offline")
    with pytest.raises(LibraryError, match="already exists"):
        research.create("thesis", folder, "litreview-offline")
    with pytest.raises(LibraryError, match="folder not found"):
        research.create("other", tmp_path / "missing", "litreview-offline")
    with pytest.raises(ValueError):
        research.create("Bad Name", folder, "litreview-offline")


async def test_index_search_and_resume(research: Research, folder: Path) -> None:
    research.create("thesis", folder, "litreview-offline")
    emb = HashEmbedder()
    report = await research.index("thesis", emb)
    assert (report.added, report.failed, report.chunks > 4) == (2, 0, True)
    docs = {d.path: d for d in research.documents("thesis")}
    assert set(docs) == {"sleep.pdf", "sub/caffeine.pdf"}
    assert docs["sleep.pdf"].title == "Sleep and Memory" and docs["sleep.pdf"].pages == 3

    hits = await research.search("thesis", "does sleep improve working memory", emb, k=3)
    assert hits[0].path == "sleep.pdf" and 1 <= hits[0].page_start <= hits[0].page_end <= 3
    # keyword ranking finds an exact rare term
    hits = await research.search("thesis", "CYP1A2", emb, k=3)
    assert hits[0].path == "sub/caffeine.pdf" and hits[0].keyword_rank == 1

    calls = len(emb.calls)
    again = await research.index("thesis", emb)
    assert (again.unchanged, again.added, again.chunks) == (2, 0, 0)
    assert len(emb.calls) == calls  # nothing re-embedded


async def test_changed_missing_and_pruned(research: Research, folder: Path) -> None:
    research.create("thesis", folder, "litreview-offline")
    emb = HashEmbedder()
    await research.index("thesis", emb)
    (folder / "sleep.pdf").write_bytes(make_pdf(paper("Sleep revised", 2), title="Sleep v2"))
    (folder / "sub" / "caffeine.pdf").unlink()
    report = await research.index("thesis", emb)
    assert (report.updated, report.missing) == (1, 1)
    statuses = {d.path: d.status for d in research.documents("thesis")}
    assert statuses == {"sleep.pdf": "indexed", "sub/caffeine.pdf": "missing"}
    hits = await research.search("thesis", "caffeine reaction time", emb, k=5)
    assert all(h.path == "sleep.pdf" for h in hits)  # old chunks and vectors are gone
    # the file comes back unchanged: available again without re-embedding
    (folder / "sub" / "caffeine.pdf").write_bytes(make_pdf(
        [[f"Caffeine reaction time trial {i}: caffeine improved reaction time in shift workers "
          "(CYP1A2 genotype)." for i in range(30)]] * 2, title="Caffeine Trial"))  # fmt: skip
    calls = len(emb.calls)
    back = await research.index("thesis", emb)
    assert back.unchanged == 2 and len(emb.calls) == calls
    hits = await research.search("thesis", "CYP1A2", emb, k=1)
    assert hits[0].path == "sub/caffeine.pdf"
    (folder / "sub" / "caffeine.pdf").unlink()
    await research.index("thesis", emb, prune=True)
    assert [d.path for d in research.documents("thesis")] == ["sleep.pdf"]


async def test_scanned_and_broken_files(research: Research, folder: Path) -> None:
    (folder / "scan.pdf").write_bytes(make_pdf([[""], [""]]))
    (folder / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
    research.create("thesis", folder, "litreview-offline")
    report = await research.index("thesis", HashEmbedder())
    assert (report.no_text, report.failed, report.added) == (1, 1, 2)
    st = {d.path: d.status for d in research.documents("thesis")}
    assert st["scan.pdf"] == "no_text" and st["broken.pdf"] == "failed"


async def test_model_change_needs_rebuild(research: Research, folder: Path) -> None:
    research.create("thesis", folder, "litreview-offline")
    await research.index("thesis", HashEmbedder(dim=64))
    (folder / "new.pdf").write_bytes(make_pdf(paper("New paper", 1)))
    with pytest.raises(LibraryError, match="--rebuild"):
        await research.index("thesis", HashEmbedder(dim=32, model_name="cw/other:embed"))
    report = await research.index("thesis", HashEmbedder(dim=32, model_name="cw/other:embed"),
                                  rebuild=True)  # fmt: skip
    assert report.added == 3
    assert research.get("thesis").embedding_dim == 32


async def test_search_needs_index_and_matching_model(research: Research, folder: Path) -> None:
    research.create("thesis", folder, "litreview-offline")
    with pytest.raises(LibraryError, match="isn't indexed"):
        await research.search("thesis", "x", HashEmbedder())
    await research.index("thesis", HashEmbedder())
    with pytest.raises(LibraryError, match="same model"):
        await research.search("thesis", "x", HashEmbedder(model_name="cw/x:embed"))


@pytest.mark.parametrize("query", ['AND OR NOT ( "', "near(a b)", "*", "sleep* ^memory", ""])
async def test_odd_queries_dont_break_search(research: Research, folder: Path, query: str) -> None:
    research.create("thesis", folder, "litreview-offline")
    await research.index("thesis", HashEmbedder())
    await research.search("thesis", query, HashEmbedder(), k=2)


def test_fts_query() -> None:
    assert fts_query('Sleep "and" memory!') == '"sleep" OR "and" OR "memory"'
    assert fts_query("!!") is None


def test_remove_drops_index(research: Research, folder: Path) -> None:
    research.create("thesis", folder, "litreview-offline")
    research.remove("thesis")
    assert research.list_libraries() == []
    assert (folder / "sleep.pdf").exists()


def test_research_schema_matches_migrations(research: Research) -> None:
    with research._db.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": lambda o, n, t, r, c: not (
            t == "table" and n and unmanaged_table(n))})  # fmt: skip
        assert compare_metadata(ctx, METADATA["research"]) == []


def test_data_folder_is_private(research: Research, paths: Paths) -> None:
    import sys

    if sys.platform != "win32":
        assert paths.data_dir.stat().st_mode & 0o777 == 0o700


# ------------------------------------------------------------------ embedder (policy + audit)


async def test_in_process_embedder_is_audited(paths: Paths) -> None:
    cfg = load_config(paths)
    fake = FakeOllama()
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fake.handler))
    with AuditLog.open(paths) as audit:
        emb = InProcessEmbedder(cfg, PolicyEngine(cfg, audit=audit), backends,
                                environment="litreview-offline", batch_size=2)  # fmt: skip
        vecs = await emb.embed(["a", "b", "c"])
        assert len(vecs) == 3 and len(fake.requests) == 2  # batched
        recs = audit.records()
        assert [r.phase for r in recs] == ["pre", "post", "pre", "post"]
        assert recs[0].details["operation"] == "embeddings" and recs[0].actor == "cairnward"


async def test_in_process_embedder_respects_policy(paths: Paths) -> None:
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "backends.toml").write_text(
        '[backends.cloud]\ndriver = "openai_compat"\nurl = "https://openrouter.ai/api/v1"\n'
        'location = "hosted"\n[roles.main]\nbackend = "cloud"\nmodel = "m"\n'
        '[roles.embed]\nbackend = "cloud"\nmodel = "e"\n'
    )
    cfg = load_config(paths)  # litreview-offline: hosted denied (network off)
    emb = InProcessEmbedder(cfg, PolicyEngine(cfg), Backends(cfg.backends),
                            environment="litreview-offline")  # fmt: skip
    with pytest.raises(EmbeddingDenied):
        await emb.embed(["secret unpublished text"])


# ------------------------------------------------------------------ CLI end to end


class _Embed(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        vecs = []
        for t in req["input"]:
            v = [0.0] * 32
            for w in t.lower().split():
                v[int(hashlib.md5(w.encode(), usedforsecurity=False).hexdigest(), 16) % 32] += 1
            n = math.sqrt(sum(x * x for x in v)) or 1
            vecs.append([x / n for x in v])
        body = json.dumps({"embeddings": vecs, "prompt_eval_count": 5}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_cli_end_to_end(paths: Paths, folder: Path, monkeypatch: pytest.MonkeyPatch,
                        capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    from cairnward.cli import main

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Embed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "backends.toml").write_text(
        f'[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:{srv.server_address[1]}"\n'
        '[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[roles.embed]\nbackend = "local-ollama"\nmodel = "e"\n'
    )
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    base = ["--config-dir", str(paths.config_dir), "library"]
    try:
        assert main([*base, "create", "thesis", str(folder)]) == 0
        assert main([*base, "index", "thesis"]) == 0
        out = capsys.readouterr().out
        assert "2 added" in out and "sleep.pdf: 3 pages" in out
        assert main([*base, "search", "thesis", "working memory sleep", "-k", "2", "--json"]) == 0
        hits = json.loads(capsys.readouterr().out)
        assert hits[0]["path"] == "sleep.pdf"
        assert main([*base, "list"]) == 0
        assert "thesis" in capsys.readouterr().out
    finally:
        srv.shutdown()
    with AuditLog.open(paths) as audit:
        actions = [r.action for r in audit.records()]
        assert "library.index" in actions and "model.call" in actions
        assert audit.verify().ok
