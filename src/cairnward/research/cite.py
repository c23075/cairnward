"""Reference formatting: APA 7 and IEEE (spec §4.6, FR-RES-6).

Input is CSL-JSON, what Zotero and most reference managers export. Output is Markdown
(*italics*) or plain text. The two shipped styles are implemented here directly, for
the common item types: journal articles, books, chapters, conference papers, reports,
theses, web pages and preprints. This built-in formatter is the fallback: with a CSL
engine installed (citeproc-js or pandoc, see research.csl), the official CSL style files
are used instead, and any other `.csl` style works too.

Titles are used as given: converting to APA's sentence case automatically would break
proper nouns, so keep them in sentence case in your reference manager.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

Item = dict[str, Any]
MONTHS = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.", "Jul.", "Aug.", "Sep.", "Oct.",
          "Nov.", "Dec."]  # fmt: skip
EN_DASH = "–"


class CiteError(Exception):
    pass


# ------------------------------------------------------------------ helpers


def _year(item: Item) -> str | None:
    issued = item.get("issued") or {}
    parts = issued.get("date-parts") or [[]]
    if parts and parts[0]:
        return str(parts[0][0])
    raw = issued.get("raw") or issued.get("literal")
    m = re.search(r"\d{4}", str(raw or ""))
    return m.group(0) if m else None


def _month(item: Item) -> int | None:
    parts = (item.get("issued") or {}).get("date-parts") or [[]]
    return int(parts[0][1]) if parts and len(parts[0]) > 1 else None


APA_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]  # fmt: skip


def _apa_date(item: Item) -> str | None:
    parts = ((item.get("issued") or {}).get("date-parts") or [[]])[0]
    if len(parts) < 2 or not 1 <= int(parts[1]) <= 12:
        return None
    out = f"{parts[0]}, {APA_MONTHS[int(parts[1]) - 1]}"
    return f"{out} {int(parts[2])}" if len(parts) > 2 else out


def _initials(given: str, *, spaced: bool) -> str:
    out = []
    for part in re.split(r"\s+", given.strip()):
        if not part:
            continue
        hyph = [p[0] + "." for p in part.split("-") if p]
        out.append("-".join(hyph))
    return " ".join(out) if spaced else "".join(out)


def _pages(item: Item) -> str | None:
    pages = item.get("page")
    return str(pages).replace("--", EN_DASH).replace("-", EN_DASH) if pages else None


def _doi(item: Item) -> str | None:
    doi = item.get("DOI")
    if not doi:
        return None
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", str(doi).strip(), flags=re.I)
    return doi


def _it(text: str, md: bool) -> str:
    return f"*{text}*" if md and text else text


def _end(text: str, mark: str = ".") -> str:
    text = text.rstrip()
    return text if text.endswith((".", "?", "!")) else text + mark


# ------------------------------------------------------------------ APA 7


def _apa_name(n: dict[str, Any]) -> str:
    if "literal" in n:
        return str(n["literal"])
    given = _initials(str(n.get("given", "")), spaced=True)
    family = " ".join(x for x in (n.get("non-dropping-particle"), n.get("family")) if x)
    return f"{family}, {given}".rstrip(", ") if given else family


def _apa_authors(names: list[dict[str, Any]]) -> str:
    ns = [_apa_name(n) for n in names]
    if not ns:
        return ""
    if len(ns) == 1:
        return ns[0]
    if len(ns) <= 20:
        return ", ".join(ns[:-1]) + ", & " + ns[-1]
    return ", ".join(ns[:19]) + ", . . . " + ns[-1]


def _apa_editors(names: list[dict[str, Any]]) -> str:
    def one(n: dict[str, Any]) -> str:
        if "literal" in n:
            return str(n["literal"])
        return f"{_initials(str(n.get('given', '')), spaced=True)} {n.get('family', '')}".strip()

    ns = [one(n) for n in names]
    joined = ns[0] if len(ns) == 1 else (" & ".join(ns) if len(ns) == 2
                                          else ", ".join(ns[:-1]) + ", & " + ns[-1])  # fmt: skip
    return f"{joined} ({'Ed.' if len(ns) == 1 else 'Eds.'})"


def apa(item: Item, *, md: bool = True) -> str:
    kind = item.get("type", "article-journal")
    authors = item.get("author") or []
    title = str(item.get("title", "")).strip()
    year = _year(item) or "n.d."
    if kind in ("webpage", "post", "post-weblog", "article-newspaper", "article-magazine"):
        year = _apa_date(item) or year  # APA 7: (2024, June 26) for dated web pages
    lead = _apa_authors(authors)
    head = f"{_end(lead)} ({year})." if lead else f"{_it(title, md)}. ({year})."
    doi = _doi(item)
    link = f"https://doi.org/{doi}" if doi else (item.get("URL") or "")
    container = str(item.get("container-title", "")).strip()
    publisher = str(item.get("publisher", "")).strip()
    pages = _pages(item)
    parts: list[str] = [head]
    if kind in ("book", "report", "thesis"):
        t = title
        extra = []
        if item.get("edition"):
            extra.append(f"{item['edition']} ed.")
        if kind == "report" and item.get("number"):
            extra.append(f"Report No. {item['number']}")
        t = f"{_it(t, md)} ({', '.join(extra)})" if extra else _it(t, md)
        if lead:
            parts.append(_end(t))
        if kind == "thesis":
            genre = item.get("genre") or "Doctoral dissertation"
            parts.append(f"[{genre}, {publisher}]." if publisher else f"[{genre}].")
        elif publisher:
            parts.append(_end(publisher))
    elif kind == "chapter":
        parts.append(_end(title))
        ed = item.get("editor") or []
        where = "In " + (f"{_apa_editors(ed)}, " if ed else "") + _it(container, md)
        if pages:
            where += f" (pp. {pages})"
        parts.append(_end(where))
        if publisher:
            parts.append(_end(publisher))
    elif kind == "paper-conference":
        parts.append(_end(title))
        where = f"In {_it(container, md)}" if container else ""
        if pages:
            where += f" (pp. {pages})"
        if where:
            parts.append(_end(where))
        if publisher:
            parts.append(_end(publisher))
    elif kind == "webpage":
        parts.append(_end(_it(title, md)))
        if container:
            parts.append(_end(container))
    else:  # article-journal, article, preprint and unknown: journal-article layout
        if lead:
            parts.append(_end(title))
        src = _it(container, md)
        vol, issue = item.get("volume"), item.get("issue")
        if vol:
            src += ", " + _it(str(vol), md)
            if issue:
                src += f"({issue})"
        elif issue:
            src += f", ({issue})"
        if pages:
            src += f", {pages}"
        elif item.get("number"):
            src += f", Article {item['number']}"
        if src.strip():
            parts.append(_end(src))
    if link:
        parts.append(link)
    return " ".join(p for p in parts if p).strip()


def apa_in_text(item: Item, *, page: str | None = None) -> str:
    page = page.replace("-", EN_DASH) if page else page
    names = item.get("author") or []
    fam = [str(n.get("family") or n.get("literal") or "") for n in names]
    if not fam:
        who = str(item.get("title", "Untitled"))
    elif len(fam) == 1:
        who = fam[0]
    elif len(fam) == 2:
        who = f"{fam[0]} & {fam[1]}"
    else:
        who = f"{fam[0]} et al."
    loc = f", p. {page}" if page and EN_DASH not in page and "-" not in page else (
        f", pp. {page}" if page else "")  # fmt: skip
    return f"({who}, {_year(item) or 'n.d.'}{loc})"


def _fold(text: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def apa_sort_key(item: Item) -> tuple[str, str, str]:
    names = item.get("author") or []
    first = str(names[0].get("family") or names[0].get("literal") or "") if names else ""
    title = str(item.get("title", ""))
    return _fold(first or title).lower(), _year(item) or "", _fold(title).lower()


# ------------------------------------------------------------------ IEEE


def _ieee_name(n: dict[str, Any]) -> str:
    if "literal" in n:
        return str(n["literal"])
    given = _initials(str(n.get("given", "")), spaced=True)
    return f"{given} {n.get('family', '')}".strip()


def _ieee_authors(names: list[dict[str, Any]]) -> str:
    ns = [_ieee_name(n) for n in names]
    if not ns:
        return ""
    if len(ns) > 6:
        return ns[0] + " et al."
    if len(ns) == 1:
        return ns[0]
    if len(ns) == 2:
        return f"{ns[0]} and {ns[1]}"
    return ", ".join(ns[:-1]) + ", and " + ns[-1]


def ieee(item: Item, n: int, *, md: bool = True) -> str:
    kind = item.get("type", "article-journal")
    who = _ieee_authors(item.get("author") or [])
    title = str(item.get("title", "")).strip()
    year = _year(item) or "n.d."
    month = _month(item)
    container = str(item.get("container-title", "")).strip()
    publisher = str(item.get("publisher", "")).strip()
    place = str(item.get("publisher-place", "")).strip()
    pages = _pages(item)
    doi = _doi(item)
    lead = f"[{n}] " + (f"{who}, " if who else "")
    if kind in ("book", "report", "thesis"):
        out = lead + _it(title, md)
        if item.get("edition"):
            out += f", {item['edition']} ed"
        out += ". "
        pub = f"{place}: {publisher}" if place and publisher else publisher or place
        if kind == "thesis":
            out += f"{item.get('genre') or 'Ph.D. dissertation'}, {publisher or ''}, ".replace(
                ", , ", ", "
            )
            out += f"{year}."
        else:
            out += f"{pub}, {year}." if pub else f"{year}."
    elif kind == "chapter":
        out = lead + f"“{title},” in {_it(container, md)}"
        if item.get("editor"):
            eds = item["editor"]
            out += ", " + _ieee_authors(eds) + (", Ed." if len(eds) == 1 else ", Eds.")
        else:
            out += "."
        pub = f"{place}: {publisher}" if place and publisher else publisher or place
        out += f" {pub}, {year}" if pub else f" {year}"
        out += f", pp. {pages}." if pages else "."
    elif kind == "paper-conference":
        out = lead + f"“{title},” in {_it(container, md)}, {year}"
        out += f", pp. {pages}" if pages else ""
        out += "."
    elif kind == "webpage":
        out = lead + f"\u201c{title},\u201d {container + '. ' if container else ''}".rstrip()
        out = out.rstrip(",") + ("" if out.endswith(".") else ".")
        acc = (item.get("accessed") or {}).get("date-parts") or [[]]
        if acc and acc[0] and len(acc[0]) == 3:
            y, m, d = acc[0]
            out += f" Accessed: {MONTHS[m - 1]} {d}, {y}."
        if item.get("URL"):
            out += f" [Online]. Available: {item['URL']}"
    else:
        out = lead + f"“{title},” {_it(container, md)}"
        if item.get("volume"):
            out += f", vol. {item['volume']}"
        if item.get("issue"):
            out += f", no. {item['issue']}"
        if pages:
            out += f", pp. {pages}" if EN_DASH in pages else f", p. {pages}"
        out += f", {MONTHS[month - 1]} {year}" if month else f", {year}"
        out += "."
    if doi:
        out = out.rstrip(".") + f", doi: {doi}."
    return out


# ------------------------------------------------------------------ API


def load_items(path: Path) -> list[Item]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise CiteError("expected a CSL-JSON list of items")
    return [x for x in data if isinstance(x, dict)]


def bibliography(items: Iterable[Item], style: str = "apa", *, md: bool = True,
                 engine: str = "builtin", paths: Any = None) -> list[str]:  # fmt: skip
    return format_references(items, style, md=md, engine=engine, paths=paths)[0]


def format_references(
    items: Iterable[Item],
    style: str = "apa",
    *,
    md: bool = True,
    engine: str = "auto",
    paths: Any = None,
) -> tuple[list[str], str]:
    """Formatted entries and the engine that made them (see research.csl). With
    engine = "auto", a CSL engine that fails falls back to the built-in formatter for
    APA and IEEE rather than leaving the bibliography empty."""
    from cairnward.config.paths import default_paths
    from cairnward.research import csl

    items = list(items)
    paths = paths or default_paths()
    try:
        chosen = csl.choose(engine, style, paths)
    except csl.CslError as exc:
        raise CiteError(str(exc)) from None
    if chosen == "builtin":
        return _builtin(items, style, md), "builtin"
    try:
        return csl.format_with(chosen, items, style, paths, md=md), chosen
    except (csl.CslError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if engine == "auto" and style.lower() in csl.SHIPPED:
            return _builtin(items, style, md), f"builtin ({chosen} failed: {str(exc)[:120]})"
        raise CiteError(str(exc)) from None


def _builtin(items: list[Item], style: str, md: bool) -> list[str]:
    s = style.lower()
    if s == "apa":
        return [apa(i, md=md) for i in sorted(items, key=apa_sort_key)]
    if s == "ieee":
        return [ieee(i, n, md=md) for n, i in enumerate(items, 1)]
    raise CiteError(f"unknown style '{style}': apa, ieee, or a .csl file")


def from_documents(rows: Iterable[Any], folder: Path | None = None) -> list[Item]:
    """CSL-JSON for a library's papers: from a `<file>.csl.json` next to the PDF when there
    is one (written by `research add` and the Zotero import), else from the PDF's own
    metadata (title, authors, year), a starting point to complete in a reference manager."""
    out: list[Item] = []
    for d in rows:
        sidecar = folder / f"{d.path}.csl.json" if folder is not None else None
        if sidecar is not None and sidecar.is_file():
            try:
                given = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                given = None
            if isinstance(given, dict) and given.get("title"):
                out.append({**given, "id": f"doc{d.id}"})
                continue
        item: Item = {"id": f"doc{d.id}", "type": "article-journal",
                      "title": d.title or Path(d.path).stem, "note": f"file: {d.path}"}  # fmt: skip
        if d.authors:
            item["author"] = [{"literal": a.strip()} for a in re.split(r";|\band\b", d.authors)
                              if a.strip()]  # fmt: skip
        if d.year:
            item["issued"] = {"date-parts": [[int(d.year)]]}
        out.append(item)
    return out
