"""GROBID for paper metadata and reference lists (D68): title, authors, year, journal and
DOI of each paper, and its reference list as structured entries.

GROBID is a Java service you run yourself, usually its official container with no network:

    docker run --rm --network none -p 127.0.0.1:8070:8070 grobid/grobid:<version>

then set `[research] grobid_url = "http://127.0.0.1:8070"` and switch it on per library
(`cairnward library set <name> --metadata grobid`). Cairnward sends each new or changed
PDF to it while indexing, with consolidation off (GROBID then makes no calls of its own to
Crossref or other services). The results are kept in Cairnward's data folder
(`library-meta/<library>/<file>.csl.json` and `.refs.json`); metadata that came with the
paper (Zotero, `research add`) takes precedence over GROBID's.
"""

from __future__ import annotations

import contextlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

TEI = {"t": "http://www.tei-c.org/ns/1.0"}
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"


class GrobidError(Exception):
    pass


def check_url(url: str) -> str:
    host = urlsplit(url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise GrobidError("grobid_url must be on this machine (e.g. http://127.0.0.1:8070): "
                          "papers are sent to it")  # fmt: skip
    return url.rstrip("/")


class Grobid:
    def __init__(
        self,
        url: str,
        *,
        proxy: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 300,
    ) -> None:
        self.url = check_url(url)
        self._client = httpx.Client(proxy=proxy, transport=transport, trust_env=False,
                                    timeout=timeout)  # fmt: skip

    def close(self) -> None:
        self._client.close()

    def alive(self) -> bool:
        try:
            return self._client.get(f"{self.url}/api/isalive").status_code == 200
        except httpx.HTTPError:
            return False

    def process(self, pdf: Path) -> str:
        """The TEI XML for one PDF (header and references; consolidation off)."""
        try:
            with pdf.open("rb") as fh:
                resp = self._client.post(
                    f"{self.url}/api/processFulltextDocument",
                    files={"input": (pdf.name, fh, "application/pdf")},
                    data={"consolidateHeader": "0", "consolidateCitations": "0",
                          "includeRawCitations": "1"},
                )  # fmt: skip
        except httpx.HTTPError as exc:
            raise GrobidError(f"GROBID isn't answering at {self.url}: {exc}") from None
        if resp.status_code == 204:
            raise GrobidError("GROBID found nothing to extract (scanned PDF?)")
        if resp.status_code != 200:
            raise GrobidError(f"GROBID answered {resp.status_code}: {resp.text[:200]}")
        return resp.text


# ------------------------------------------------------------------ TEI -> CSL-JSON


def _text(el: ET.Element | None) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _names(parent: ET.Element | None) -> list[dict[str, str]]:
    out = []
    for author in parent.findall("t:author", TEI) if parent is not None else []:
        pers = author.find("t:persName", TEI)
        if pers is None:
            continue
        family = _text(pers.find("t:surname", TEI))
        given = " ".join(_text(f) + ("." if len(_text(f)) == 1 else "")
                         for f in pers.findall("t:forename", TEI))  # fmt: skip
        if family:
            out.append({"family": family, "given": given} if given else {"family": family})
    return out


def bibl_to_csl(bs: ET.Element) -> dict[str, Any]:
    """One TEI <biblStruct> (the paper itself or one of its references) as CSL-JSON."""
    analytic, monogr = bs.find("t:analytic", TEI), bs.find("t:monogr", TEI)
    item: dict[str, Any] = {}
    a_title = _text(analytic.find("t:title", TEI)) if analytic is not None else ""
    m_title_el = monogr.find("t:title", TEI) if monogr is not None else None
    m_title, level = _text(m_title_el), (m_title_el.get("level") if m_title_el is not None else "")
    meeting = monogr is not None and monogr.find("t:meeting", TEI) is not None
    if a_title:
        item["title"] = a_title
        if m_title:
            item["container-title"] = m_title
        item["type"] = ("paper-conference" if meeting else "chapter" if level == "m"
                        else "article-journal")  # fmt: skip
    elif m_title:
        item["title"], item["type"] = m_title, "book"
    authors = _names(analytic) or _names(monogr)
    if authors:
        item["author"] = authors
    editors = [{"family": _text(e.find("t:persName/t:surname", TEI))}
               for e in (monogr.findall("t:editor", TEI) if monogr is not None else [])
               if _text(e.find("t:persName/t:surname", TEI))]  # fmt: skip
    if editors:
        item["editor"] = editors
    for idno in bs.iter("{http://www.tei-c.org/ns/1.0}idno"):
        if (idno.get("type") or "").upper() == "DOI" and _text(idno):
            item["DOI"] = _text(idno)
            break
    imprint = monogr.find("t:imprint", TEI) if monogr is not None else None
    if imprint is not None:
        date = imprint.find("t:date", TEI)
        when = (date.get("when") if date is not None else "") or _text(date)
        if m := re.match(r"(\d{4})(?:-(\d{2}))?", when or ""):
            parts = [int(m.group(1))] + ([int(m.group(2))] if m.group(2) else [])
            item["issued"] = {"date-parts": [parts]}
        for scope in imprint.findall("t:biblScope", TEI):
            unit = scope.get("unit")
            if unit == "volume":
                item["volume"] = _text(scope)
            elif unit == "issue":
                item["issue"] = _text(scope)
            elif unit == "page":
                lo, hi = scope.get("from"), scope.get("to")
                item["page"] = f"{lo}-{hi}" if lo and hi else (lo or _text(scope))
        if publisher := _text(imprint.find("t:publisher", TEI)):
            item["publisher"] = publisher
    raw = bs.find("t:note[@type='raw_reference']", TEI)
    if raw is not None and _text(raw):
        item["note"] = _text(raw)
    return item


def parse_tei(xml_text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the paper's CSL-JSON, its references as CSL-JSON)."""
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - expat refuses entity expansion attacks
    except ET.ParseError as exc:
        raise GrobidError(f"GROBID sent invalid XML: {exc}") from None
    head = root.find("t:teiHeader/t:fileDesc/t:sourceDesc/t:biblStruct", TEI)
    paper = bibl_to_csl(head) if head is not None else {}
    title = _text(root.find("t:teiHeader/t:fileDesc/t:titleStmt/t:title", TEI))
    if title and not paper.get("title"):
        paper["title"] = title
    abstract = _text(root.find("t:teiHeader/t:profileDesc/t:abstract", TEI))
    if abstract:
        paper["abstract"] = abstract
    if "issued" not in paper:
        date = root.find("t:teiHeader/t:fileDesc/t:publicationStmt/t:date", TEI)
        if date is not None and (m := re.match(r"(\d{4})", date.get("when") or _text(date))):
            paper["issued"] = {"date-parts": [[int(m.group(1))]]}
    paper.setdefault("type", "article-journal")
    refs: list[dict[str, Any]] = []
    for bs in root.iterfind(".//t:listBibl/t:biblStruct", TEI):
        ref = bibl_to_csl(bs)
        if ref.get("title") or ref.get("note"):
            ref["id"] = bs.get(XML_ID) or f"b{len(refs)}"
            refs.append(ref)
    return paper, refs


# ------------------------------------------------------------------ storing results


def meta_dir(data_dir: Path, library: str) -> Path:
    return data_dir / "library-meta" / library


def store(data_dir: Path, library: str, rel: str, paper: dict[str, Any],
          refs: list[dict[str, Any]]) -> None:  # fmt: skip
    base = meta_dir(data_dir, library) / rel
    base.parent.mkdir(parents=True, exist_ok=True)
    paper = {**paper, "note": "metadata: GROBID"}
    base.with_name(base.name + ".csl.json").write_text(json.dumps(paper, indent=1), "utf-8")
    base.with_name(base.name + ".refs.json").write_text(json.dumps(refs, indent=1), "utf-8")


def load(data_dir: Path, library: str, rel: str) -> tuple[dict[str, Any] | None, list[Any]]:
    base = meta_dir(data_dir, library) / rel
    paper: dict[str, Any] | None = None
    refs: list[Any] = []
    with contextlib.suppress(OSError, ValueError):
        paper = json.loads(base.with_name(base.name + ".csl.json").read_text("utf-8"))
    with contextlib.suppress(OSError, ValueError):
        refs = json.loads(base.with_name(base.name + ".refs.json").read_text("utf-8"))
    return paper, refs
