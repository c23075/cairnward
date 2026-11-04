"""Online academic sources (FR-RES-8/9): parsing each API's answer, de-duplication across
sources, the cache, the policy hook and safe PDF downloads. Uses synthetic responses
(tests/data/sources/README.md) through a mock transport: nothing leaves the machine."""

import json
from pathlib import Path

import httpx
import pytest

from cairnward.research import sources as src

DATA = Path(__file__).parent / "data" / "sources"
PDF = b"%PDF-1.4\n% synthetic\n"


def responder(calls: list[str]) -> httpx.MockTransport:
    def handle(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append(url)
        host = req.url.host
        if host == "api.openalex.org" and "/works/doi:" in url:
            work = json.loads((DATA / "openalex.json").read_text())["results"][0]
            return httpx.Response(200, json=work)
        files = {"api.openalex.org": "openalex.json", "api.semanticscholar.org":
                 "semantic-scholar.json", "export.arxiv.org": "arxiv.xml",
                 "api.crossref.org": "crossref.json"}  # fmt: skip
        if host in files:
            return httpx.Response(200, text=(DATA / files[host]).read_text())
        if "esearch.fcgi" in url:
            return httpx.Response(200, text=(DATA / "pubmed-esearch.json").read_text())
        if "esummary.fcgi" in url:
            return httpx.Response(200, text=(DATA / "pubmed-esummary.json").read_text())
        if url == "https://example-repository.org/papers/jsr99001.pdf":
            return httpx.Response(302, headers={"location": "/files/jsr99001-final.pdf"})
        if url.endswith("/files/jsr99001-final.pdf"):
            return httpx.Response(200, content=PDF)
        if host == "login.example":
            return httpx.Response(200, text="<html>please sign in</html>")
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def test_parsers_normalise_each_source() -> None:
    oa = src.parse_openalex(json.loads((DATA / "openalex.json").read_text()))
    assert oa[0].doi == "10.1111/jsr.99001" and oa[0].venue == "Journal of Sleep Research"
    assert oa[0].abstract == "Longer sleep predicted better recall." and oa[0].cited_by == 41
    assert oa[0].pdf_url and oa[1].doi is None and oa[1].venue is None
    s2 = src.parse_semantic_scholar(json.loads((DATA / "semantic-scholar.json").read_text()))
    assert s2[0].pmid == "35000001" and s2[1].arxiv == "2101.00001"
    ax = src.parse_arxiv((DATA / "arxiv.xml").read_text())
    assert ax[0].id == "2101.00001" and ax[0].title == "Slow-wave sleep and memory consolidation"
    assert ax[0].authors == ["Lena Ito", "Kai Berg"] and ax[0].year == 2021
    cr = src.parse_crossref(json.loads((DATA / "crossref.json").read_text()))
    assert cr[0].abstract == "Longer sleep predicted better recall." and cr[1].kind == "book"
    assert cr[1].authors == ["Sleep Society"]
    pm = src.parse_pubmed(json.loads((DATA / "pubmed-esummary.json").read_text()))
    assert pm[0].authors == ["Walker, M. P."] and pm[0].pdf_url and pm[0].year == 2022
    csl = pm[0].csl()
    assert csl["author"] == [{"family": "Walker", "given": "M. P."}] and csl["DOI"]
    assert oa[0].csl()["author"][0] == {"family": "Walker", "given": "Mara P."}


def test_parsers_tolerate_junk() -> None:
    assert src.parse_openalex({"results": [None, {"title": None}, 3]}) == []
    assert src.parse_crossref({"message": {"items": [{"title": []}]}}) == []
    assert src.parse_pubmed({"result": {"uids": ["1"], "1": "x"}}) == []
    with pytest.raises(src.SourceError):
        src.parse_arxiv("<not xml")


async def test_search_dedups_caches_and_checks_policy(tmp_path: Path) -> None:
    calls: list[str] = []
    checked: list[tuple[str, str]] = []

    async def check(source: str, url: str) -> None:
        checked.append((source, httpx.URL(url).host))
        if source == "crossref":
            raise src.SourceError("crossref: refused by the policy (network = allowlist)")

    s = src.Searcher(tmp_path / "cache", transport=responder(calls), check=check)
    try:
        out = await s.search(list(src.SOURCES), "sleep working memory", limit=5)
        report = out["sources"]
        assert "refused" in report["crossref"]["error"]
        assert report["openalex"] == {"found": 2, "cached": False}
        titles = [r["title"] for r in out["records"]]
        # the same paper from openalex, semantic scholar and pubmed: once
        assert titles.count("Sleep duration and working memory in older adults") == 1
        merged = next(r for r in out["records"] if r["doi"] == "10.1111/jsr.99001")
        assert merged["cited_by"] == 44 and merged["pmid"] == "35000001"
        assert {h for _, h in checked} >= {"api.openalex.org", "eutils.ncbi.nlm.nih.gov"}
        n = len(calls)
        again = await s.search(["openalex", "pubmed"], "sleep working memory", limit=5)
        assert len(calls) == n  # all from the cache
        assert again["sources"]["openalex"]["cached"] is True
    finally:
        await s.aclose()


async def test_lookup_and_pdf_download(tmp_path: Path) -> None:
    calls: list[str] = []
    checked: list[str] = []

    async def check(source: str, url: str) -> None:
        checked.append(httpx.URL(url).host)

    s = src.Searcher(tmp_path / "cache", transport=responder(calls), check=check)
    try:
        rec = await s.lookup("doi:10.1111/jsr.99001")
        assert rec.title.startswith("Sleep duration") and rec.pdf_url
        assert await s.fetch_pdf(rec) == PDF
        assert checked.count("example-repository.org") == 2  # the redirect is checked too
        assert src.slug(rec) == "walker-2022-sleep-duration-and-working"
        arx = await s.lookup("arxiv:2101.00001")
        assert arx.arxiv == "2101.00001"
        login = src.Record("x", "1", "Paywalled", pdf_url="https://login.example/p.pdf")
        with pytest.raises(src.SourceError, match="didn't send a PDF"):
            await s.fetch_pdf(login)
        with pytest.raises(src.SourceError, match="no open-access PDF"):
            await s.fetch_pdf(src.Record("x", "2", "No PDF"))
        with pytest.raises(src.SourceError, match="isn't a DOI"):
            await s.lookup("hello")
    finally:
        await s.aclose()


def test_unknown_source() -> None:
    with pytest.raises(src.SourceError, match="unknown source"):
        src.requests_for("google-scholar", "q", 5)


async def test_oversized_answer_is_refused_and_not_cached(tmp_path: Path) -> None:
    big = b"x" * (src.MAX_ANSWER_BYTES + 1)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=big))
    s = src.Searcher(tmp_path / "cache", transport=transport)
    try:
        with pytest.raises(src.SourceError, match="larger than"):
            await s.get("openalex", "https://api.openalex.org/works?search=x")
    finally:
        await s.aclose()
    assert not (tmp_path / "cache").exists()
