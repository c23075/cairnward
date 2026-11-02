"""Online academic sources (FR-RES-8/9): OpenAlex, Semantic Scholar, arXiv, Crossref and
PubMed. Opt-in per environment (`research_sources = ["openalex", ...]`), cached, audited.

Every request is checked by the policy engine first, as a `net.connect` to the source's
host in the environment (so `network = "off"` refuses it, `"ask"` asks, an allow-list
must list the host), and the query is scanned like any other outgoing text (redaction).
The request itself leaves through Cairnward's egress (or, in the isolated core, the
sandbox's proxy, which leads to egress). Answers are cached in the data folder for a week,
so repeating a search doesn't touch the network.

Results are normalised to `Record` and can be turned into CSL-JSON for citations. Open-
access PDFs (`pdf_url`) can be fetched into a library: size-limited, checked to be a PDF.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

SOURCES = ("openalex", "semantic-scholar", "arxiv", "crossref", "pubmed")
HOSTS = {"openalex": "api.openalex.org", "semantic-scholar": "api.semanticscholar.org",
         "arxiv": "export.arxiv.org", "crossref": "api.crossref.org",
         "pubmed": "eutils.ncbi.nlm.nih.gov"}  # fmt: skip
CACHE_DAYS = 7
MAX_PDF_BYTES = 50 * 1024 * 1024
USER_AGENT = "Cairnward (literature review; https://github.com/c23075/possessed-incantation)"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


class SourceError(Exception):
    pass


@dataclass
class Record:
    source: str
    id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    pmid: str | None = None
    abstract: str | None = None
    url: str | None = None
    pdf_url: str | None = None  # an open-access PDF, when the source knows one
    cited_by: int | None = None
    kind: str = "article-journal"

    def key(self) -> str:
        """For de-duplicating across sources: the DOI, else a folded title + year."""
        if self.doi:
            return "doi:" + self.doi.lower()
        return "t:" + re.sub(r"\W+", "", self.title.lower())[:80] + f":{self.year or ''}"

    def csl(self) -> dict[str, Any]:
        item: dict[str, Any] = {"id": self.id, "type": self.kind, "title": self.title}
        names = []
        for a in (x.strip() for x in self.authors):
            if not a:
                continue
            if "," in a:  # "Family, Given"
                family, given = (x.strip() for x in a.split(",", 1))
            else:  # "Given Family"
                given, _, family = a.rpartition(" ")
            names.append({"family": family, "given": given} if family and given
                         else {"literal": a})  # fmt: skip
        if names:
            item["author"] = names
        if self.year:
            item["issued"] = {"date-parts": [[self.year]]}
        if self.venue:
            item["container-title"] = self.venue
        if self.doi:
            item["DOI"] = self.doi
        if self.url:
            item["URL"] = self.url
        if self.abstract:
            item["abstract"] = self.abstract
        if self.arxiv and self.kind == "article":
            item["number"] = f"arXiv:{self.arxiv}"
            item["publisher"] = "arXiv"
        return item


def _doi(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I)


def _year(value: Any) -> int | None:
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(value or ""))
    return int(m.group(1)) if m else None


def _text(value: Any, limit: int = 4000) -> str | None:
    if not isinstance(value, str):
        return None
    clean = re.sub(r"<[^>]+>", " ", value)  # JATS/HTML tags in abstracts
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit] or None


# ------------------------------------------------------------------ the sources


def _openalex_abstract(inverted: Any) -> str | None:
    if not isinstance(inverted, dict):
        return None
    words: dict[int, str] = {}
    for word, positions in inverted.items():
        for p in positions if isinstance(positions, list) else []:
            if isinstance(p, int):
                words[p] = str(word)
    return _text(" ".join(words[i] for i in sorted(words)))


def parse_openalex(data: Any) -> list[Record]:
    out = []
    for w in (data or {}).get("results", []) if isinstance(data, dict) else []:
        if not isinstance(w, dict) or not (w.get("title") or w.get("display_name")):
            continue
        loc = (w.get("primary_location") or {}).get("source") or {}
        best = w.get("best_oa_location") or {}
        kind = {"book-chapter": "chapter", "book": "book", "dissertation": "thesis",
                "preprint": "article"}.get(str(w.get("type")), "article-journal")  # fmt: skip
        out.append(Record(
            "openalex", str(w.get("id", "")).rsplit("/", 1)[-1],
            str(w.get("title") or w.get("display_name")).strip(),
            [str((a.get("author") or {}).get("display_name", "")) for a in
             w.get("authorships") or [] if isinstance(a, dict)],
            w.get("publication_year") if isinstance(w.get("publication_year"), int) else None,
            loc.get("display_name"), _doi(w.get("doi")), None, None,
            _openalex_abstract(w.get("abstract_inverted_index")),
            w.get("doi") or w.get("id"), best.get("pdf_url"),
            w.get("cited_by_count") if isinstance(w.get("cited_by_count"), int) else None, kind,
        ))  # fmt: skip
    return out


def parse_semantic_scholar(data: Any) -> list[Record]:
    out = []
    for p in (data or {}).get("data", []) if isinstance(data, dict) else []:
        if not isinstance(p, dict) or not p.get("title"):
            continue
        ext = p.get("externalIds") or {}
        pdf = (p.get("openAccessPdf") or {}).get("url") or None
        out.append(Record(
            "semantic-scholar", str(p.get("paperId", "")), str(p["title"]).strip(),
            [str(a.get("name", "")) for a in p.get("authors") or [] if isinstance(a, dict)],
            p.get("year") if isinstance(p.get("year"), int) else None, p.get("venue") or None,
            _doi(ext.get("DOI")), ext.get("ArXiv"),
            str(ext["PubMed"]) if ext.get("PubMed") else None,
            _text(p.get("abstract")), p.get("url"), pdf,
            p.get("citationCount") if isinstance(p.get("citationCount"), int) else None,
        ))  # fmt: skip
    return out


def parse_arxiv(xml_text: str) -> list[Record]:
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - expat refuses entity expansion attacks
    except ET.ParseError as exc:
        raise SourceError(f"arXiv sent invalid XML: {exc}") from None
    out = []
    for e in root.findall("a:entry", ATOM):
        title = _text(e.findtext("a:title", "", ATOM))
        ident = (e.findtext("a:id", "", ATOM) or "").rsplit("/abs/", 1)[-1]
        if not title or not ident:
            continue
        arxiv_id = re.sub(r"v\d+$", "", ident)
        pdf = next(
            (
                ln.get("href")
                for ln in e.findall("a:link", ATOM)
                if ln.get("title") == "pdf" or ln.get("type") == "application/pdf"
            ),
            None,
        )
        out.append(Record(
            "arxiv", arxiv_id, title,
            [n.findtext("a:name", "", ATOM).strip() for n in e.findall("a:author", ATOM)],
            _year(e.findtext("a:published", "", ATOM)),
            "arXiv", _doi(e.findtext("arxiv:doi", "", ATOM)), arxiv_id, None,
            _text(e.findtext("a:summary", "", ATOM)), f"https://arxiv.org/abs/{arxiv_id}",
            pdf or f"https://arxiv.org/pdf/{arxiv_id}", None, "article",
        ))  # fmt: skip
    return out


def parse_crossref(data: Any) -> list[Record]:
    items = ((data or {}).get("message") or {}).get("items", []) if isinstance(data, dict) else []
    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        title = it["title"][0] if isinstance(it["title"], list) else it["title"]
        authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
                   for a in it.get("author") or [] if isinstance(a, dict)]  # fmt: skip
        parts = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        venue = it.get("container-title")
        pdf = next((ln.get("URL") for ln in it.get("link") or [] if isinstance(ln, dict)
                    and ln.get("content-type") == "application/pdf"), None)  # fmt: skip
        kind = {"journal-article": "article-journal", "book-chapter": "chapter", "book": "book",
                "proceedings-article": "paper-conference", "posted-content": "article",
                "dissertation": "thesis", "report": "report"}.get(str(it.get("type")),
                                                                  "article-journal")  # fmt: skip
        out.append(Record(
            "crossref", str(it.get("DOI", "")), str(title).strip(), authors,
            parts[0] if parts and isinstance(parts[0], int) else None,
            (venue[0] if isinstance(venue, list) and venue else venue) or None,
            _doi(it.get("DOI")), None, None, _text(it.get("abstract")), it.get("URL"), pdf,
            it.get("is-referenced-by-count") if isinstance(it.get("is-referenced-by-count"),
                                                           int) else None, kind,
        ))  # fmt: skip
    return out


def _pubmed_name(name: str) -> str:
    """PubMed writes "Walker MP": family name, then initials."""
    m = re.fullmatch(r"(.+?)\s+([A-Z]{1,4})", name.strip())
    if not m:
        return name
    return f"{m.group(1)}, {' '.join(c + '.' for c in m.group(2))}"


def parse_pubmed(summary: Any) -> list[Record]:
    result = (summary or {}).get("result", {}) if isinstance(summary, dict) else {}
    out = []
    for uid in result.get("uids", []) if isinstance(result, dict) else []:
        s = result.get(uid)
        if not isinstance(s, dict) or not s.get("title"):
            continue
        ids = {a.get("idtype"): a.get("value") for a in s.get("articleids") or []
               if isinstance(a, dict)}  # fmt: skip
        pmc = ids.get("pmc")
        out.append(Record(
            "pubmed", str(uid), _text(s["title"]) or "",
            [_pubmed_name(str(a.get("name", ""))) for a in s.get("authors") or []
             if isinstance(a, dict) and a.get("authtype", "Author") == "Author"],
            _year(s.get("pubdate")), s.get("fulljournalname") or s.get("source"),
            _doi(ids.get("doi")), None, str(uid), None,
            f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc}/pdf/" if pmc else None,
        ))  # fmt: skip
    return out


def requests_for(source: str, query: str, limit: int) -> list[str]:
    q = quote(query)
    if source == "openalex":
        return [f"https://api.openalex.org/works?search={q}&per-page={limit}"]
    if source == "semantic-scholar":
        fields = "title,year,authors,venue,externalIds,abstract,openAccessPdf,citationCount,url"
        return [f"https://api.semanticscholar.org/graph/v1/paper/search?query={q}&limit={limit}"
                f"&fields={fields}"]  # fmt: skip
    if source == "arxiv":
        return [f"https://export.arxiv.org/api/query?search_query=all:{q}&max_results={limit}"]
    if source == "crossref":
        return [f"https://api.crossref.org/works?query={q}&rows={limit}"]
    if source == "pubmed":
        return [f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={q}"
                f"&retmax={limit}&retmode=json"]  # fmt: skip
    raise SourceError(f"unknown source '{source}': {', '.join(SOURCES)}")


# ------------------------------------------------------------------ searching

Check = Callable[[str, str], Any]  # (source, host) -> awaitable (allowed, reason)


class Searcher:
    """Runs searches for one environment: policy check per request, cache, normalise."""

    def __init__(self, cache_dir: Path, *, proxy: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 check: Callable[[str, str], Any] | None = None,
                 cache_days: int = CACHE_DAYS) -> None:  # fmt: skip
        self.cache_dir, self.cache_days = cache_dir, cache_days
        self.check = check  # async (source, url) -> None, raising SourceError when refused
        self._client = httpx.AsyncClient(proxy=proxy, transport=transport, trust_env=False,
                                         timeout=30, follow_redirects=False,
                                         headers={"User-Agent": USER_AGENT})  # fmt: skip

    async def aclose(self) -> None:
        await self._client.aclose()

    def _cached(self, url: str) -> Any:
        f = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - entry.get("at", 0) > self.cache_days * 86400:
            return None
        return entry

    def _store(self, url: str, body: str, kind: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        f = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")
        f.write_text(json.dumps({"at": time.time(), "kind": kind, "body": body}), encoding="utf-8")

    async def get(self, source: str, url: str, *, cache: bool = True) -> tuple[str, bool]:
        """(body, came from cache). Raises SourceError when refused or failed."""
        if cache and (hit := self._cached(url)):
            return str(hit["body"]), True
        if self.check is not None:
            await self.check(source, url)
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise SourceError(f"{source}: {type(exc).__name__}: {str(exc)[:200]}") from None
        if resp.status_code == 403 and "cairnward" in resp.text.lower():
            raise SourceError(f"{source}: refused by Cairnward's egress ({resp.text[:200]})")
        if resp.status_code != 200:
            raise SourceError(f"{source}: HTTP {resp.status_code}")
        body = resp.text
        if cache:
            self._store(url, body, resp.headers.get("content-type", ""))
        return body, False

    async def search_one(self, source: str, query: str, limit: int) -> tuple[list[Record], bool]:
        (url,) = requests_for(source, query, limit)
        body, cached = await self.get(source, url)
        try:
            if source == "arxiv":
                return parse_arxiv(body), cached
            data = json.loads(body)
            if source == "openalex":
                return parse_openalex(data), cached
            if source == "semantic-scholar":
                return parse_semantic_scholar(data), cached
            if source == "crossref":
                return parse_crossref(data), cached
            ids = [str(i) for i in ((data.get("esearchresult") or {}).get("idlist") or [])][:limit]
            if not ids:
                return [], cached
            summary_url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed"
                           f"&id={','.join(ids)}&retmode=json")  # fmt: skip
            summary, cached2 = await self.get(source, summary_url)
            return parse_pubmed(json.loads(summary)), cached and cached2
        except (ValueError, AttributeError, TypeError) as exc:
            raise SourceError(f"{source}: unexpected answer ({str(exc)[:120]})") from None

    async def search(self, sources: list[str], query: str, limit: int = 10) -> dict[str, Any]:
        """Search each source; results de-duplicated (first source wins, cited_by and
        missing fields filled in from the others)."""
        results = await asyncio.gather(*(self.search_one(s, query, limit) for s in sources),
                                       return_exceptions=True)  # fmt: skip
        merged: dict[str, Record] = {}
        report: dict[str, Any] = {}
        for source, res in zip(sources, results, strict=True):
            if isinstance(res, BaseException):
                report[source] = {"error": str(res)[:300]}
                continue
            records, cached = res
            report[source] = {"found": len(records), "cached": cached}
            for r in records:
                k = r.key()
                if k not in merged:
                    merged[k] = r
                    continue
                first = merged[k]
                for name in ("doi", "arxiv", "pmid", "abstract", "pdf_url", "venue", "year"):
                    if getattr(first, name) in (None, "") and getattr(r, name):
                        setattr(first, name, getattr(r, name))
                if (r.cited_by or 0) > (first.cited_by or 0):
                    first.cited_by = r.cited_by
        return {"records": [asdict(r) for r in merged.values()], "sources": report}

    async def lookup(self, ident: str) -> Record:
        """One work by `doi:10...`, `arxiv:2401.01234` or a bare DOI (via OpenAlex/arXiv)."""
        ident = ident.strip()
        if ident.lower().startswith("arxiv:") or re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?", ident):
            aid = ident.split(":", 1)[-1]
            url = f"https://export.arxiv.org/api/query?id_list={quote(aid)}"
            body, _ = await self.get("arxiv", url)
            found = parse_arxiv(body)
        else:
            doi = _doi(ident.split(":", 1)[1] if ident.lower().startswith("doi:") else ident)
            if not doi or "/" not in doi:
                raise SourceError(f"'{ident}' isn't a DOI or an arXiv id")
            body, _ = await self.get("openalex", f"https://api.openalex.org/works/doi:{quote(doi)}")
            found = parse_openalex({"results": [json.loads(body)]})
        if not found:
            raise SourceError(f"nothing found for {ident}")
        return found[0]

    async def fetch_pdf(self, record: Record) -> bytes:
        """The open-access PDF, size-limited, checked to be a PDF (one redirect allowed, to a
        host that is checked again)."""
        if not record.pdf_url:
            raise SourceError(f"no open-access PDF known for '{record.title[:60]}'")
        url = record.pdf_url
        for _ in range(3):
            if self.check is not None:
                await self.check("pdf", url)
            async with self._client.stream("GET", url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    url = str(httpx.URL(url).join(resp.headers["location"]))
                    continue
                if resp.status_code != 200:
                    raise SourceError(f"PDF download: HTTP {resp.status_code} from "
                                      f"{urlsplit(url).hostname}")  # fmt: skip
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise SourceError("the PDF is larger than 50 MB")
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data.startswith(b"%PDF-"):
                    raise SourceError(f"{urlsplit(url).hostname} didn't send a PDF (it may need a "
                                      "login or a browser)")  # fmt: skip
                return data
        raise SourceError("too many redirects")


def slug(record: Record) -> str:
    first = (record.authors[0].split()[-1] if record.authors else "anon").lower()
    words = re.findall(r"[a-z0-9]+", record.title.lower())[:4]
    base = "-".join([re.sub(r"[^a-z0-9]", "", first) or "anon", str(record.year or "nd"), *words])
    return base[:80]
