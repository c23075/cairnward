"""Reference formatting, APA 7 and IEEE (FR-RES-6): golden files checked by hand against
the style rules, plus the rules that are easy to get wrong."""

from pathlib import Path

import pytest

from cairnward.cli import main
from cairnward.research.cite import CiteError, apa, apa_in_text, bibliography, ieee, load_items

DATA = Path(__file__).parent / "data"


@pytest.mark.parametrize("style", ["apa", "ieee"])
def test_golden(style: str) -> None:
    items = load_items(DATA / "refs.json")
    expected = (DATA / f"refs.{style}.golden.md").read_text(encoding="utf-8").strip()
    assert "\n\n".join(bibliography(items, style)) == expected


def test_rules() -> None:
    one = {"type": "article-journal", "title": "T", "author": [{"family": "Lee", "given": "Ann"}],
           "container-title": "J", "volume": "3", "issued": {"date-parts": [[2020]]}}  # fmt: skip
    assert apa(one, md=False) == "Lee, A. (2020). T. J, 3."
    assert apa({**one, "issued": {}}, md=False).startswith("Lee, A. (n.d.).")
    many = {**one, "author": [{"family": f"N{i}", "given": "X"} for i in range(25)]}
    out = apa(many, md=False)
    assert "N18, X., . . . N24, X." in out and "N19" not in out  # APA: 19, ellipsis, last
    assert apa_in_text(one, page="12") == "(Lee, 2020, p. 12)"
    two = {**one, "author": [*one["author"], {"family": "Kim", "given": "Bo"}]}
    assert apa_in_text(two) == "(Lee & Kim, 2020)"
    lq, rq = chr(0x201C), chr(0x201D)
    assert ieee(two, 3, md=False) == f"[3] A. Lee and B. Kim, {lq}T,{rq} J, vol. 3, 2020."
    thesis = {"type": "thesis", "title": "On ice", "author": [{"family": "Ng", "given": "Kai"}],
              "publisher": "Univ. of Oslo", "issued": {"date-parts": [[2018]]}}  # fmt: skip
    assert apa(thesis, md=False) == (
        "Ng, K. (2018). On ice. [Doctoral dissertation, Univ. of Oslo]."
    )
    with pytest.raises(CiteError, match="unknown style"):
        bibliography([one], "harvard")


def test_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["cite", str(DATA / "refs.json"), "--style", "ieee", "--plain"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[1] M. P. Walker and R. Stickgold") and "*" not in out
    assert main(["cite", str(DATA / "refs.json"), "--style", "x.csl"]) == 1


def test_sidecar_metadata_is_used(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cairnward.research.cite import from_documents

    (tmp_path / "online").mkdir()
    (tmp_path / "online" / "walker-2022.pdf.csl.json").write_text(
        '{"type": "article-journal", "title": "Sleep and memory", "DOI": "10.1/x",'
        ' "author": [{"family": "Walker", "given": "M. P."}]}'
    )
    rows = [SimpleNamespace(id=1, path="online/walker-2022.pdf", title="scan.pdf", authors=None,
                            year=None),
            SimpleNamespace(id=2, path="other.pdf", title="From the PDF", authors="Kim; Lee",
                            year=2020)]  # fmt: skip
    a, b = from_documents(rows, tmp_path)
    assert a["id"] == "doc1" and a["DOI"] == "10.1/x" and a["author"][0]["family"] == "Walker"
    assert b["title"] == "From the PDF" and b["author"] == [{"literal": "Kim"}, {"literal": "Lee"}]


def test_bibtex() -> None:
    from cairnward.research.cite import to_bibtex

    bib = to_bibtex(load_items(DATA / "refs.json"))
    assert bib.count("\n@") + 1 == len(load_items(DATA / "refs.json"))
    assert "@inproceedings{Nguyen2024fast," in bib and "booktitle = {Proceedings" in bib
    assert "pages = {e13540--e13552}" in bib and "doi = {10.1111/jsr.13540}" in bib
    assert "title = {{Sleep and working memory in older adults}}" in bib  # case kept
    org = to_bibtex([{"type": "report", "title": "Costs & 50% of x_y",
                      "author": [{"literal": "World Health Organization"}]}])  # fmt: skip
    assert "author = {{World Health Organization}}" in org and r"\& 50\% of x\_y" in org
    twice = to_bibtex([{"title": "Slow waves", "author": [{"family": "Ito"}]}] * 2)
    assert "Ito" in twice and "ndslow," in twice and "ndslowb," in twice  # unique keys
