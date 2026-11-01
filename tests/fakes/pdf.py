"""A minimal PDF writer for tests (text only, Helvetica), so tests need no PDF library."""

from __future__ import annotations

import hashlib
import math


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]], *, title: str | None = None) -> bytes:
    """pages: lines of text per page."""
    objects: list[bytes] = []
    n_pages = len(pages)
    # 1 catalog, 2 pages, 3 font, then (page, content) pairs, then optional info
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    for i, lines in enumerate(pages):
        ops = ["BT", "/F1 10 Tf", "14 TL", "50 780 Td"]
        for line in lines:
            ops.append(f"({_escape(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode()
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    info_ref = ""
    if title:
        objects.append(f"<< /Title ({_escape(title)}) /CreationDate (D:20240115000000) >>".encode())
        info_ref = f" /Info {len(objects)} 0 R"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info_ref} >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


class HashEmbedder:
    """Deterministic bag-of-words embeddings: texts sharing words are similar."""

    def __init__(self, dim: int = 64, model_name: str = "cw/test:embed"):
        self.dim, self.model_name = dim, model_name
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for word in t.lower().split():
                w = word.strip(".,;:()")
                if len(w) > 2:
                    v[
                        int(hashlib.md5(w.encode(), usedforsecurity=False).hexdigest(), 16)
                        % self.dim
                    ] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out
