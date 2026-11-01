"""Research: literature libraries and (later) review workflows."""

from cairnward.research.chunking import Chunk, chunk_pages
from cairnward.research.embedder import Embedder, EmbeddingDenied, InProcessEmbedder
from cairnward.research.library import Hit, IndexReport, Library, LibraryError, Research
from cairnward.research.pdf import PdfError, PdfText, read_pdf

__all__ = [
    "Chunk",
    "Embedder",
    "EmbeddingDenied",
    "Hit",
    "InProcessEmbedder",
    "IndexReport",
    "Library",
    "LibraryError",
    "PdfError",
    "PdfText",
    "Research",
    "chunk_pages",
    "read_pdf",
]
