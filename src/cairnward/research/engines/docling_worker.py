"""Docling worker: turns PDFs into per-page text (Markdown, with tables) for Cairnward.

Runs with the Docling engine's own Python, inside the sandbox with no network at all;
models come from the folder given (downloaded at install). Standalone: no Cairnward
imports.

stdin:  {"files": ["/path/a.pdf", ...], "models": "/path/models", "out": "/work/out",
         "ocr": true}
writes: <out>/<n>.json = {"file", "pages": ["...", ...], "title"} or {"file", "error"}
stdout: a line "DONE {count}" (or "ERROR {json}").
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def convert(files: list[str], models: str, out: Path, ocr: bool) -> int:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions(artifacts_path=models, do_ocr=ocr, do_table_structure=True)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
        pipeline_options=opts)})  # fmt: skip
    done = 0
    for n, f in enumerate(files):
        result: dict[str, Any] = {"file": f}
        try:
            doc = converter.convert(f).document
            numbers = sorted(doc.pages) if getattr(doc, "pages", None) else []
            result["pages"] = [doc.export_to_markdown(page_no=p) for p in numbers] or [
                doc.export_to_markdown()]  # fmt: skip
            title = next((t.text for t in getattr(doc, "texts", [])
                          if str(getattr(t, "label", "")).endswith("title")), None)  # fmt: skip
            result["title"] = title
            done += 1
        except Exception as exc:  # one bad PDF shouldn't stop the others
            result["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        (out / f"{n}.json").write_text(json.dumps(result), encoding="utf-8")
    return done


def main() -> int:
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "DO_NOT_TRACK": "1",
                       "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS", "4")})  # fmt: skip
    spec = json.loads(sys.stdin.read())
    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)
    try:
        n = convert(list(spec["files"]), str(spec["models"]), out, bool(spec.get("ocr", True)))
    except Exception as exc:  # reported to Cairnward
        print("ERROR " + json.dumps({"error": f"{type(exc).__name__}: {exc}"[:2000]}), flush=True)
        return 1
    print(f"DONE {n}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
