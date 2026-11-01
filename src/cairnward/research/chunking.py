"""Page-aware chunking (spec D63: small chunks keep a question's prompt near 2K tokens).

Chunks are built from paragraphs (split further into sentences, then words, when too
long) up to a target size, with a small overlap, and record which pages they span so
answers can cite exact pages.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

CHARS_PER_TOKEN = 4  # rough, for English prose; good enough for sizing
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")


@dataclass(frozen=True)
class Chunk:
    seq: int
    page_start: int  # 1-based
    page_end: int
    text: str
    tokens: int


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


def _units(pages: list[str], max_chars: int) -> list[tuple[int, str]]:
    """(page, piece) where every piece fits in a chunk."""
    out: list[tuple[int, str]] = []
    for page_no, text in enumerate(pages, start=1):
        for para in re.split(r"\n\s*\n", text):
            para = " ".join(para.split())
            if not para:
                continue
            if len(para) <= max_chars:
                out.append((page_no, para))
                continue
            for sentence in _SENTENCE.split(para):
                while len(sentence) > max_chars:  # one enormous "sentence": cut at a space
                    cut = sentence.rfind(" ", 0, max_chars)
                    cut = cut if cut > max_chars // 2 else max_chars
                    out.append((page_no, sentence[:cut].strip()))
                    sentence = sentence[cut:].strip()
                if sentence:
                    out.append((page_no, sentence))
    return out


def chunk_pages(
    pages: list[str], *, target_tokens: int = 350, overlap_tokens: int = 60
) -> list[Chunk]:
    if overlap_tokens >= target_tokens:
        raise ValueError("overlap must be smaller than the chunk size")
    target = target_tokens * CHARS_PER_TOKEN
    overlap = overlap_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    current: list[tuple[int, str]] = []
    size = 0
    carried_count = 0  # leading pieces of `current` repeated from the previous chunk

    def emit() -> None:
        text = " ".join(p for _, p in current)
        chunks.append(
            Chunk(len(chunks), current[0][0], current[-1][0], text, estimate_tokens(text))
        )

    for unit in _units(pages, target):
        if current and size + 1 + len(unit[1]) > target:
            emit()
            carried: list[tuple[int, str]] = []
            carried_size = 0
            for prev in reversed(current):  # trailing pieces become the overlap
                if carried_size + len(prev[1]) > overlap:
                    break
                carried.insert(0, prev)
                carried_size += len(prev[1]) + 1
            current, size, carried_count = carried, carried_size, len(carried)
        current.append(unit)
        size += len(unit[1]) + 1
    if len(current) > carried_count:  # anything new since the last chunk
        emit()
    return chunks
