"""Per-library PDF parsing options (D68) and the indexing hooks they turn into.

    cairnward library set <name> --parser docling --metadata grobid

- parser "basic" (default): pypdfium2. "docling": the optional Docling worker (layout,
  tables, OCR), with pypdfium2 for any PDF it can't read.
- metadata "pdf" (default): the PDF's own metadata. "grobid": title, authors, DOI and the
  reference list from your GROBID service (`[research] grobid_url`).

Options live in `<data>/library-options.json`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from cairnward.config.loader import Config
from cairnward.research.pdf import PdfText

PARSERS = ("basic", "docling")
METADATA = ("pdf", "grobid")
DoclingRunner = Callable[[str, list[Path]], Awaitable[list[dict[str, Any]]]]


def options_file(data_dir: Path) -> Path:
    return data_dir / "library-options.json"


def get_options(data_dir: Path, library: str) -> dict[str, str]:
    try:
        all_opts = json.loads(options_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        all_opts = {}
    mine = all_opts.get(library) if isinstance(all_opts, dict) else None
    out = {"parser": "basic", "metadata": "pdf"}
    if isinstance(mine, dict):
        out.update({k: str(v) for k, v in mine.items() if k in out})
    return out


def set_options(data_dir: Path, library: str, **changes: str | None) -> dict[str, str]:
    try:
        all_opts = json.loads(options_file(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        all_opts = {}
    if not isinstance(all_opts, dict):
        all_opts = {}
    current = get_options(data_dir, library)
    if (p := changes.get("parser")) is not None:
        if p not in PARSERS:
            raise ValueError(f"parser must be one of {', '.join(PARSERS)}")
        current["parser"] = p
    if (m := changes.get("metadata")) is not None:
        if m not in METADATA:
            raise ValueError(f"metadata must be one of {', '.join(METADATA)}")
        current["metadata"] = m
    all_opts[library] = current
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = options_file(data_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(all_opts, indent=1), encoding="utf-8")
    tmp.replace(options_file(data_dir))
    return current


async def _local_docling(cfg: Config, environment: str, files: list[Path]) -> list[dict[str, Any]]:
    from cairnward.research.engines import docling

    return await asyncio.to_thread(docling.run_worker, cfg, environment, files)


def hooks(cfg: Config, library: str, environment: str, *,
          docling_runner: DoclingRunner | None = None, proxy: str | None = None,
          grobid_transport: httpx.BaseTransport | None = None,
          ) -> tuple[Any, Any]:  # fmt: skip
    """(parse_many, enrich) for Research.index, from the library's options."""
    opts = get_options(cfg.paths.data_dir, library)
    parse_many = enrich = None
    if opts["parser"] == "docling":

        async def parse_many_fn(files: list[Path]) -> dict[str, PdfText]:
            runner = docling_runner or (lambda env, fs: _local_docling(cfg, env, fs))
            results = await runner(environment, files)
            out: dict[str, PdfText] = {}
            for r in results:
                pages = [str(p) for p in r.get("pages") or []]
                if "error" not in r and any(p.strip() for p in pages):
                    out[str(r["file"])] = PdfText(pages, r.get("title"), None, None)
            return out

        parse_many = parse_many_fn
    if opts["metadata"] == "grobid":
        url = cfg.main.research.grobid_url
        if not url:
            raise ValueError(f"library '{library}' uses GROBID but [research] grobid_url isn't set")
        from cairnward.research import grobid

        async def enrich_fn(path: Path, rel: str) -> None:
            def work() -> None:
                client = grobid.Grobid(url, proxy=proxy, transport=grobid_transport)
                try:
                    paper, refs = grobid.parse_tei(client.process(path))
                finally:
                    client.close()
                grobid.store(cfg.paths.data_dir, library, rel, paper, refs)

            await asyncio.to_thread(work)

        enrich = enrich_fn
    return parse_many, enrich
