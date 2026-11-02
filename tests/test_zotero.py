"""Zotero import (FR-RES-7) against a fake Zotero 7 local API (the routes and fields of
Zotero's web API v3, user 0): collections by path, paging, the three ways an attachment's
file is found, metadata sidecars, re-runs copying only changes, items without a PDF."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.research.zotero import Zotero, ZoteroError, import_items

PDF_A = b"%PDF-1.4 paper A"
PDF_B = b"%PDF-1.4 paper B"


def fake_zotero(tmp: Path, *, page_size_seen: list[int]) -> httpx.MockTransport:
    storage = tmp / "storage"
    (storage / "ATTA").mkdir(parents=True)
    (storage / "ATTA" / "walker.pdf").write_bytes(PDF_A)
    linked = tmp / "elsewhere" / "ito.pdf"
    linked.parent.mkdir()
    linked.write_bytes(PDF_B)
    redirected = tmp / "redirected.pdf"
    redirected.write_bytes(b"%PDF-1.4 paper C")
    collections = [
        {"key": "C1", "data": {"name": "Thesis", "parentCollection": False}},
        {"key": "C2", "data": {"name": "Chapter 2", "parentCollection": "C1"}},
        {"key": "C3", "data": {"name": "Chapter 2", "parentCollection": False}},
    ]

    def item(key: str, title: str, family: str, year: int) -> dict[str, Any]:
        return {"key": key, "data": {"itemType": "journalArticle", "title": title},
                "csljson": {"type": "article-journal", "title": title, "id": f"x/{key}",
                            "author": [{"family": family, "given": "A."}],
                            "issued": {"date-parts": [[year]]}}}  # fmt: skip

    items = [item("ITA", "Sleep and memory", "Walker", 2022),
             item("ITB", "Slow waves", "Ito", 2021),
             item("ITC", "Naps", "Ruiz", 2019),
             item("ITD", "No file here", "Kim", 2020)]  # fmt: skip
    children = {
        "ITA": [{"key": "ATTA", "data": {"itemType": "attachment", "contentType":
                 "application/pdf", "path": "storage:walker.pdf"}}],
        "ITB": [{"key": "ATTB", "data": {"itemType": "attachment", "contentType":
                 "application/pdf", "path": str(linked)}}],
        "ITC": [{"key": "ATTN", "data": {"itemType": "note"}},
                {"key": "ATTC", "data": {"itemType": "attachment", "contentType":
                 "application/pdf", "path": ""}}],
        "ITD": [{"key": "ATTD", "data": {"itemType": "attachment", "contentType": "text/html"}}],
    }  # fmt: skip

    def handle(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/users/0")
        limit, start = int(req.url.params.get("limit", 100)), int(req.url.params.get("start", 0))
        page_size_seen.append(limit)

        def page(rows: list[dict[str, Any]]) -> httpx.Response:
            return httpx.Response(200, json=rows[start:start + 2],  # Zotero pages; here 2 per page
                                  headers={"Total-Results": str(len(rows))})  # fmt: skip

        if path == "/collections":
            return page(collections)
        if path == "/collections/C2/items/top":
            return page(items)
        if path.startswith("/items/") and path.endswith("/children"):
            return page(children[path.split("/")[2]])
        if path == "/items/ATTC/file":
            return httpx.Response(302, headers={"location": f"file://{redirected}"})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def test_import_a_collection(tmp_path: Path) -> None:
    seen: list[int] = []
    z = Zotero(storage=tmp_path / "storage", transport=fake_zotero(tmp_path, page_size_seen=seen))
    lib = tmp_path / "library"
    try:
        assert {c["name"] for c in z.collections()} == {"Thesis", "Thesis/Chapter 2", "Chapter 2"}
        with pytest.raises(ZoteroError, match="ambiguous"):
            z.find_collection("Chapter 2")
        report = import_items(z, lib, "Thesis/Chapter 2")
        assert report == {"items": 4, "copied": 3, "unchanged": 0, "no_pdf": ["No file here"]}
        files = sorted(p.name for p in (lib / "zotero").glob("*.pdf"))
        assert files == ["ito-2021-slow-waves-itb.pdf", "ruiz-2019-naps-itc.pdf",
                         "walker-2022-sleep-and-memory-ita.pdf"]  # fmt: skip
        assert (lib / "zotero" / files[2]).read_bytes() == PDF_A
        meta = json.loads((lib / "zotero" / (files[2] + ".csl.json")).read_text())
        assert meta["id"] == "ITA" and meta["author"][0]["family"] == "Walker"
        again = import_items(z, lib, "thesis/chapter 2")  # names match case-insensitively
        assert again["copied"] == 0 and again["unchanged"] == 3
    finally:
        z.close()


def test_errors_are_clear(tmp_path: Path) -> None:
    with pytest.raises(ZoteroError, match="local API on this machine"):
        Zotero("http://zotero.example.org/api")

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    z = Zotero(transport=httpx.MockTransport(down))
    with pytest.raises(ZoteroError, match="Allow other applications"):
        z.collections()
    z.close()
    seen: list[int] = []
    z = Zotero(storage=tmp_path / "storage", transport=fake_zotero(tmp_path, page_size_seen=seen))
    with pytest.raises(ZoteroError, match="no Zotero collection"):
        z.find_collection("Nope")
    z.close()
