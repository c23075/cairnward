"""CSL engines (FR-RES-6): citeproc-js and pandoc give the same output with the vendored
official APA 7 and IEEE styles (golden files reviewed by hand); `auto` picks the best one
installed and falls back to the built-in formatter; vendored files are hash-checked."""

import shutil
from pathlib import Path
from typing import Any

import pytest

from cairnward.config.paths import Paths
from cairnward.research import csl
from cairnward.research.cite import CiteError, format_references, load_items

DATA = Path(__file__).parent / "data"
ITEMS = load_items(DATA / "refs.json")


@pytest.fixture(scope="session")
def citeproc_paths(tmp_path_factory: pytest.TempPathFactory) -> Paths:
    """A data folder with citeproc-js installed (pinned; needs Node and the npm registry)."""
    d = tmp_path_factory.mktemp("citeproc")
    paths = Paths(d / "cfg", d / "data", d / "state")
    if not shutil.which("npm") or csl.node_version() is None:
        pytest.skip("needs Node.js >= 20 and npm")
    try:
        csl.install_citeproc_js(paths, log=lambda m: None)
    except Exception as exc:  # offline
        pytest.skip(f"couldn't install citeproc-js: {exc}")
    return paths


def golden(style: str) -> str:
    return (DATA / f"refs.{style}.csl.golden.md").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("style", ["apa", "ieee"])
def test_citeproc_js_golden(citeproc_paths: Paths, style: str) -> None:
    out, used = format_references(ITEMS, style, engine="citeproc-js", paths=citeproc_paths)
    assert used == "citeproc-js"
    assert "\n\n".join(out) == golden(style)


@pytest.mark.skipif(csl.pandoc() is None, reason="pandoc not installed")
@pytest.mark.parametrize("style", ["apa", "ieee"])
def test_pandoc_golden(tmp_path: Path, style: str) -> None:
    out, used = format_references(ITEMS, style, engine="pandoc",
                                  paths=Paths(tmp_path, tmp_path, tmp_path))  # fmt: skip
    assert used == "pandoc"
    assert "\n\n".join(out) == golden(style)


def test_the_rules_the_engines_get_right(citeproc_paths: Paths) -> None:
    out = "\n".join(format_references(ITEMS, "apa", engine="citeproc-js",
                                      paths=citeproc_paths)[0])  # fmt: skip
    assert out.index("Álvarez") < out.index("Chen")  # accents sort with their letter
    assert "(Ed.)" in out and "https://doi.org/https" not in out  # DOI prefix normalised
    ieee = format_references(ITEMS, "ieee", engine="citeproc-js", paths=citeproc_paths)[0]
    assert ieee[0].startswith("[1] M. P. Walker") and "adults,”" in ieee[0]  # comma inside
    plain = format_references(ITEMS, "apa", md=False, engine="citeproc-js",
                              paths=citeproc_paths)[0]  # fmt: skip
    assert not any("*" in e for e in plain)


def test_auto_prefers_citeproc_js(citeproc_paths: Paths) -> None:
    assert csl.choose("auto", "apa", citeproc_paths) == "citeproc-js"
    out, used = format_references(ITEMS, "apa", engine="auto", paths=citeproc_paths)
    assert used == "citeproc-js" and "\n\n".join(out) == golden("apa")


def test_auto_without_csl_engines_uses_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(csl, "pandoc", lambda: None)
    paths = Paths(tmp_path, tmp_path, tmp_path)
    out, used = format_references(ITEMS, "apa", engine="auto", paths=paths)
    assert used == "builtin" and out[0].startswith("A, A., B, B.")
    with pytest.raises(CiteError, match="need citeproc-js"):
        format_references(ITEMS, str(tmp_path / "x.csl"), engine="auto", paths=paths)
    with pytest.raises(CiteError, match="citeproc-js isn't installed"):
        format_references(ITEMS, "apa", engine="citeproc-js", paths=paths)
    with pytest.raises(CiteError, match="apa and ieee only"):
        format_references(ITEMS, str(tmp_path / "x.csl"), engine="builtin", paths=paths)


def test_auto_falls_back_when_an_engine_fails(
    citeproc_paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*a: Any, **k: Any) -> list[str]:
        raise csl.CslError("boom")

    monkeypatch.setattr(csl, "run_citeproc_js", broken)
    out, used = format_references(ITEMS, "apa", engine="auto", paths=citeproc_paths)
    assert used.startswith("builtin (citeproc-js failed: boom") and out
    with pytest.raises(CiteError, match="boom"):  # an explicit choice reports the failure
        format_references(ITEMS, "apa", engine="citeproc-js", paths=citeproc_paths)


def test_vendored_files_are_hash_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "csl"
    shutil.copytree(csl.HERE, fake)
    monkeypatch.setattr(csl, "HERE", fake)
    assert csl.style_file("apa") == fake / "styles" / "apa.csl"
    with (fake / "styles" / "apa.csl").open("a", encoding="utf-8") as fh:
        fh.write("<!-- changed -->")
    with pytest.raises(csl.CslError, match="SHA-256"):
        csl.style_file("apa")


def test_doi_and_ids_prepared() -> None:
    out = csl.prepare([{"DOI": "https://doi.org/10.1/x"}, {"id": 7, "DOI": "doi: 10.2/y"}])
    assert [(i["id"], i["DOI"]) for i in out] == [("item0", "10.1/x"), ("7", "10.2/y")]


def test_html_to_markdown() -> None:
    html = (
        '<div class="csl-entry"><div class="csl-left-margin">[2]</div>'
        '<div class="csl-right-inline">A. B, <i>Title</i>, &#38; more.</div></div>'
    )
    assert csl.to_text(html, md=True) == "[2] A. B, *Title*, & more."
    assert csl.to_text(html, md=False) == "[2] A. B, Title, & more."
