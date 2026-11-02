"""Richer PDF parsing (D68): GROBID metadata and reference lists, the Docling worker, the
per-library options and the indexing hooks (fallback to pypdfium2, failures not fatal).
GROBID and Docling themselves are stand-ins here (see tests/data/grobid/paper.tei.xml and
the fake `docling` package below); the build environment can't run them."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.research import Research, grobid
from cairnward.research.cite import from_documents
from cairnward.research.parsing import get_options, hooks, set_options
from tests.fakes.pdf import HashEmbedder, make_pdf

TEI = (Path(__file__).parent / "data" / "grobid" / "paper.tei.xml").read_text()


def test_tei_to_csl() -> None:
    paper, refs = grobid.parse_tei(TEI)
    assert paper["title"] == "Sleep duration and working memory in older adults"
    assert paper["author"] == [{"family": "Walker", "given": "Mara P."},
                               {"family": "Stickgold", "given": "Robert"}]  # fmt: skip
    assert paper["DOI"] == "10.1111/jsr.99001" and paper["container-title"] == (
        "Journal of Sleep Research")  # fmt: skip
    assert paper["issued"] == {"date-parts": [[2022, 6]]} and paper["page"] == "13540-13552"
    assert paper["volume"] == "31" and paper["issue"] == "4"
    assert paper["abstract"].startswith("Longer sleep")
    assert [r["type"] for r in refs] == ["article-journal", "book", "paper-conference"]
    assert refs[0]["note"].startswith("Born J.") and refs[1]["publisher"] == "Scribner"
    assert [r["id"] for r in refs] == ["b0", "b1", "b2"]
    with pytest.raises(grobid.GrobidError, match="invalid XML"):
        grobid.parse_tei("<nope")


def test_grobid_client(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/isalive":
            return httpx.Response(200, text="true")
        body = req.read()
        seen.append({"path": req.url.path, "pdf": b"%PDF" in body,
                     "no_consolidation": b'name="consolidateHeader"\r\n\r\n0' in body})  # fmt: skip
        return httpx.Response(200, text=TEI)

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(make_pdf([["x"]]))
    g = grobid.Grobid("http://127.0.0.1:8070", transport=httpx.MockTransport(handle))
    assert g.alive() and grobid.parse_tei(g.process(pdf))[0]["DOI"] == "10.1111/jsr.99001"
    assert seen == [{"path": "/api/processFulltextDocument", "pdf": True,
                     "no_consolidation": True}]  # fmt: skip
    g.close()
    with pytest.raises(grobid.GrobidError, match="on this machine"):
        grobid.Grobid("https://grobid.example.org")


@pytest.fixture
def lib(tmp_path: Path) -> tuple[Paths, Path]:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "cairnward.toml").write_text(
        '[research]\ngrobid_url = "http://127.0.0.1:8070"\n'
    )
    papers = tmp_path / "papers"
    papers.mkdir()
    for name in ("a", "b"):
        (papers / f"{name}.pdf").write_bytes(
            make_pdf(
                [[f"basic text of {name} line {i}" for i in range(20)]] * 2, title=f"Paper {name}"
            )
        )
    with Research.open(paths) as r:
        r.create("thesis", papers, "litreview-offline")
    return paths, papers


def test_options(lib: tuple[Paths, Path]) -> None:
    paths, _ = lib
    assert get_options(paths.data_dir, "thesis") == {"parser": "basic", "metadata": "pdf"}
    set_options(paths.data_dir, "thesis", parser="docling")
    assert set_options(paths.data_dir, "thesis", metadata="grobid") == {
        "parser": "docling", "metadata": "grobid"}  # fmt: skip
    with pytest.raises(ValueError, match="parser must be"):
        set_options(paths.data_dir, "thesis", parser="ocrmypdf")


async def test_index_with_docling_and_grobid(lib: tuple[Paths, Path]) -> None:
    paths, papers = lib
    cfg = load_config(paths)
    set_options(paths.data_dir, "thesis", parser="docling", metadata="grobid")
    asked: list[list[str]] = []

    async def fake_docling(env: str, files: list[Path]) -> list[dict[str, Any]]:
        asked.append(sorted(f.name for f in files))
        text = "| Table | from Docling |\n\n" + "Docling text of a. " * 30
        return [{"file": str(f), "pages": [text]}
                if f.name == "a.pdf" else {"file": str(f), "error": "can't read"}
                for f in files]  # fmt: skip

    calls: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if len(calls) == 2:
            return httpx.Response(500, text="GROBID crashed")  # second paper: not fatal
        return httpx.Response(200, text=TEI)

    parse_many, enrich = hooks(cfg, "thesis", "litreview-offline", docling_runner=fake_docling,
                               grobid_transport=httpx.MockTransport(handle))  # fmt: skip
    emb = HashEmbedder(model_name="cw/litreview-offline:embed")
    with Research.open(paths) as r:
        report = await r.index("thesis", emb, parse_many=parse_many, enrich=enrich)
        assert report.added == 2 and asked == [["a.pdf", "b.pdf"]]
        assert any("metadata" in e for e in report.errors)  # GROBID's failure reported
        rows = {d.path: d for d in r.documents("thesis")}
        a_text = " ".join(c.text for c in r.chunk_rows("thesis", [rows["a.pdf"].id]))
        b_text = " ".join(c.text for c in r.chunk_rows("thesis", [rows["b.pdf"].id]))
        assert "Docling text of a" in a_text and "basic text of b" in b_text  # b fell back
        again = await r.index("thesis", emb, parse_many=parse_many, enrich=enrich)
        assert again.unchanged == 2 and asked == [["a.pdf", "b.pdf"]]  # nothing re-read
        docs = r.documents("thesis")
    # GROBID's metadata is used for citations; a sidecar next to the PDF would win
    meta = lambda rel: grobid.load(paths.data_dir, "thesis", rel)[0]  # noqa: E731
    items = {i["id"]: i for i in from_documents(docs, papers, meta)}
    a_item = items[f"doc{rows['a.pdf'].id}"]
    assert a_item["DOI"] == "10.1111/jsr.99001" and a_item["note"] == "metadata: GROBID"
    assert grobid.load(paths.data_dir, "thesis", "a.pdf")[1][0]["id"] == "b0"
    (papers / "a.pdf.csl.json").write_text(json.dumps({"title": "From Zotero", "type": "book"}))
    assert {i["title"] for i in from_documents(docs, papers, meta)} >= {"From Zotero"}


def test_grobid_needs_its_url(lib: tuple[Paths, Path]) -> None:
    paths, _ = lib
    (paths.config_dir / "cairnward.toml").write_text("")
    set_options(paths.data_dir, "thesis", metadata="grobid")
    with pytest.raises(ValueError, match="grobid_url"):
        hooks(load_config(paths), "thesis", "litreview-offline")


FAKE_DOCLING = {
    "docling/__init__.py": "",
    "docling/datamodel/__init__.py": "",
    "docling/datamodel/base_models.py": "class InputFormat:\n    PDF = 'pdf'\n",
    "docling/datamodel/pipeline_options.py": textwrap.dedent("""
        class PdfPipelineOptions:
            def __init__(self, artifacts_path, do_ocr, do_table_structure):
                import os
                assert os.path.isdir(artifacts_path), artifacts_path
                assert os.environ.get("HF_HUB_OFFLINE") == "1"
                self.ocr = do_ocr
        """),
    "docling/document_converter.py": textwrap.dedent("""
        class PdfFormatOption:
            def __init__(self, pipeline_options):
                self.options = pipeline_options

        class _Doc:
            def __init__(self, path):
                self.pages = {1: None, 2: None}
                self.texts = []
                self.path = path
            def export_to_markdown(self, page_no=None):
                return f"page {page_no} of {self.path.split('/')[-1]} | a | table |"

        class _Result:
            def __init__(self, path):
                self.document = _Doc(path)

        class DocumentConverter:
            def __init__(self, format_options):
                pass
            def convert(self, path):
                if "broken" in path:
                    raise ValueError("cannot parse")
                return _Result(path)
        """),
}


def test_docling_worker_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real worker script with a stand-in `docling` package in the engine's venv."""
    from cairnward.research.engines import docling

    if sys.platform == "win32":
        pytest.skip("POSIX venv layout")
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    monkeypatch.setenv("CAIRNWARD_CACHE_DIR", str(tmp_path / "cache"))
    root = docling.engine_dir(paths)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")], check=True)
    site = next((root / "venv" / "lib").glob("python*/site-packages"))
    for rel, code in FAKE_DOCLING.items():
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        (site / rel).write_text(code)
    (root / "models").mkdir()
    cfg = load_config(paths)
    good, broken = tmp_path / "good.pdf", tmp_path / "broken.pdf"
    good.write_bytes(b"%PDF")
    broken.write_bytes(b"%PDF")
    out = docling.run_worker(cfg, "litreview-offline", [good, broken], sandbox=False)
    assert out[0]["pages"] == ["page 1 of good.pdf | a | table |",
                               "page 2 of good.pdf | a | table |"]  # fmt: skip
    assert "cannot parse" in out[1]["error"]


async def test_control_endpoint_runs_docling_for_the_core() -> None:
    from cairnward.egress import Sessions
    from cairnward.egress.control import control_app

    async def docling(env: str, files: list[Path]) -> list[dict[str, Any]]:
        return [{"file": str(f), "pages": [env]} for f in files]

    app = control_app(Sessions(), docling=docling)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://egress") as c:  # fmt: skip
        r = await c.post("/v1/engines/docling/parse",
                         json={"environment": "e", "files": ["/x/a.pdf"]})  # fmt: skip
        assert r.json() == {"results": [{"file": str(Path("/x/a.pdf")), "pages": ["e"]}]}
    bare = control_app(Sessions())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bare),
                                 base_url="http://egress") as c:  # fmt: skip
        r = await c.post("/v1/engines/docling/parse", json={"environment": "e", "files": ["/a"]})
        assert r.status_code == 404
