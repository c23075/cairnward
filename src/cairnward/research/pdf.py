"""PDF text extraction with pypdfium2 (Apache-2.0/BSD; spec D53: no AGPL parsers)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium


class PdfError(Exception):
    pass


@dataclass(frozen=True)
class PdfText:
    pages: list[str]  # cleaned text per page, page 1 first
    title: str | None
    authors: str | None
    year: int | None

    @property
    def has_text(self) -> bool:
        return sum(len(p.strip()) for p in self.pages) >= 50


_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_SPACES = re.compile("[ \t\u00a0]+")
_BLANKS = re.compile(r"\n{3,}")


def clean(text: str) -> str:
    """Normalise line breaks, rejoin words hyphenated at line ends, tidy spaces."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    # pdfium marks a word hyphenated across lines with \x02 (and sometimes \ufffe)
    text = text.replace("\x02\n", "").replace("\x02", "").replace("\ufffe", "")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def _title(meta_title: str | None, first_page: str) -> str | None:
    if (
        meta_title
        and meta_title.strip()
        and not re.match(r"(?i)^(microsoft word|untitled)", meta_title)
    ):
        return meta_title.strip()[:300]
    for line in first_page.split("\n"):
        if len(line.strip()) >= 8:
            return line.strip()[:300]
    return None


def read_pdf(path: Path) -> PdfText:
    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        raise PdfError(f"can't open PDF ({exc}); encrypted or damaged?") from exc
    try:
        meta = pdf.get_metadata_dict(skip_empty=True)
        pages = []
        for i in range(len(pdf)):
            page = pdf[i]
            textpage = page.get_textpage()
            pages.append(clean(textpage.get_text_bounded()))
            textpage.close()
            page.close()
    finally:
        pdf.close()
    year = None
    if m := re.match(r"D:(\d{4})", meta.get("CreationDate", "")):
        year = int(m.group(1))
    return PdfText(
        pages,
        _title(meta.get("Title"), pages[0] if pages else ""),
        (meta.get("Author") or "").strip() or None,
        year,
    )
