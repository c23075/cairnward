"""Citation and quotation checks (spec §4.6, FR-RES-5).

- Quotations: every quoted passage ("...", 12+ characters) must appear in the papers'
  text. In a lit.ask answer it should be in the passage it cites; found only elsewhere
  in the same paper is reported as a weaker match; not found at all is a problem.
  Matching ignores case, whitespace, quote and dash styles and soft hyphens.
- References (drafts): each entry of a References/Bibliography section must match a
  paper in the library by its title. A DOI is only checked for form here: resolving it
  needs the network (a research step with egress, later).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# An opening mark follows no word and precedes text; a closing mark is the reverse. So a
# stray mark doesn't pair with the next quotation and turn the text between into a "quote".
_QUOTE = re.compile(r"(?<!\w)[\"“„](?=\S)([^\"“”„\n]{12,600})(?<=\S)[\"”“](?!\w)")
_CITES_AFTER = re.compile(r"^\W{0,4}((?:\[\d+(?:\s*[,–\-]\s*\d+)*\]\s*)+)")
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_REF_HEADING = re.compile(r"^\s*(?:#+\s*)?(references|bibliography|works cited|sources)\s*:?\s*$",
                          re.IGNORECASE | re.MULTILINE)  # fmt: skip
_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
    "−": "-", "­": None, " ": " ",
})  # fmt: skip


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    return re.sub(r"\s+", " ", text).strip().lower()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalise(text))


@dataclass
class QuoteCheck:
    quote: str
    status: str  # "in_cited" | "same_paper" | "elsewhere" | "not_found"
    where: str | None = None  # "title, p. N"
    cited: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("in_cited", "elsewhere")


@dataclass
class RefCheck:
    entry: str
    status: str  # "in_library" | "doi_unverified" | "not_found"
    match: str | None = None


@dataclass
class DraftReport:
    quotes: list[QuoteCheck] = field(default_factory=list)
    references: list[RefCheck] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return sum(not q.ok for q in self.quotes) + sum(
            r.status == "not_found" for r in self.references
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "quotes": [asdict(q) | {"ok": q.ok} for q in self.quotes],
            "references": [asdict(r) for r in self.references],
            "problems": self.problems,
        }


def quotations(text: str) -> list[tuple[str, list[int]]]:
    """Quoted passages and the [n] citations right after each. The refusal sentence lit.ask
    asks for is shown in quotes in its prompt; a model repeating them quotes no paper."""
    from cairnward.research.ask import CANT, cited_numbers

    out = []
    for m in _QUOTE.finditer(text):
        if normalise(m.group(1)).strip(" .,;:") == CANT:
            continue
        after = _CITES_AFTER.match(text[m.end() : m.end() + 60])
        out.append((m.group(1), cited_numbers(after.group(1)) if after else []))
    return out


def _find(quote: str, rows: list[Any]) -> Any | None:
    """The chunk row whose text contains the quote (after normalising)."""
    needle = normalise(quote).strip(" .,;:")
    found = [row for row in rows if needle in normalise(row.text)]
    if found:  # the narrowest page range is the most precise location
        return min(found, key=lambda r: (r.page_end - r.page_start, r.page_start))
    # a quote may straddle two chunks: try each document's joined text
    by_doc: dict[int, list[Any]] = {}
    for row in rows:
        by_doc.setdefault(row.document_id, []).append(row)
    for doc_rows in by_doc.values():
        joined = normalise(" ".join(r.text for r in doc_rows))
        if needle in joined:
            return doc_rows[0]
    return None


def _label(row: Any) -> str:
    title = row.title or Path(row.path).name
    pages = (
        str(row.page_start)
        if row.page_start == row.page_end
        else f"{row.page_start}-{row.page_end}"
    )
    return f"{title}, p. {pages}"


def check_answer_quotes(text: str, hits: list[Any], library_rows: list[Any]) -> list[QuoteCheck]:
    """Quotes in a lit.ask answer against the passages given (hits) and their papers."""
    out = []
    for quote, cited in quotations(text):
        cited_hits = [hits[n - 1] for n in cited if 1 <= n <= len(hits)]
        hit = _find(quote, cited_hits)
        if hit is not None:
            out.append(QuoteCheck(quote, "in_cited", _label(hit), cited))
            continue
        if cited:  # with no citation there's no "passage cited" for it to be missing from
            docs = {h.document_id for h in cited_hits} or {h.document_id for h in hits}
            row = _find(quote, [r for r in library_rows if r.document_id in docs])
            if row is not None:
                out.append(QuoteCheck(quote, "same_paper", _label(row), cited))
                continue
        row = _find(quote, library_rows)
        status = "elsewhere" if row is not None and not cited else "not_found"
        out.append(QuoteCheck(quote, status, _label(row) if row is not None else None, cited))
    return out


def check_references(text: str, documents: list[Any]) -> list[RefCheck]:
    m = _REF_HEADING.search(text)
    if not m:
        return []
    section = text[m.end() :]
    stop = re.search(r"^\s*#+\s", section, re.MULTILINE)  # the next heading ends it
    section = section[: stop.start()] if stop else section
    entries = [e.strip(" -*\t\n") for e in re.split(r"\n\s*\n|\n(?=\s*(?:[-*]|\[\d+\]|\d+\.)\s)",
                                                  section) if e.strip()]  # fmt: skip
    titles = [(d, set(_words(d.title or ""))) for d in documents if d.title]
    out = []
    for entry in entries:
        words = set(_words(entry))
        best, score = None, 0.0
        for doc, tw in titles:
            if len(tw) >= 2:
                s = len(tw & words) / len(tw)
                if s > score:
                    best, score = doc, s
        if best is not None and score >= 0.8:
            out.append(RefCheck(entry[:300], "in_library", best.title))
        elif doi := _DOI.search(entry):
            out.append(RefCheck(entry[:300], "doi_unverified", doi.group(0).rstrip(".,;")))
        else:
            out.append(RefCheck(entry[:300], "not_found"))
    return out


def check_draft(research: Any, library: str, text: str) -> DraftReport:
    rows = research.chunk_rows(library)
    report = DraftReport()
    for quote, _cited in quotations(text):
        row = _find(quote, rows)
        report.quotes.append(QuoteCheck(quote, "elsewhere" if row else "not_found",
                                        _label(row) if row else None))  # fmt: skip
    report.references = check_references(text, research.documents(library))
    return report
