"""Zotero as a library source (FR-RES-7): read-only, through Zotero 7's local API.

Zotero must be running with "Allow other applications on this computer to communicate
with Zotero" switched on (Settings → Advanced). The local API answers on this machine only
(http://127.0.0.1:23119/api/, the same routes as Zotero's web API with user 0) and can't
change anything in Zotero. Nothing goes over the network.

`cairnward library zotero <library> --collection "Thesis"` copies each item's PDF into the
library folder (`zotero/<author-year-title>-<key>.pdf`) with Zotero's own metadata next to
it (`.csl.json`), so citations use Zotero's metadata; then the library is indexed as usual.
Re-running copies only what changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

DEFAULT_URL = "http://127.0.0.1:23119/api"
PAGE = 100
NOT_RUNNING = ("Zotero isn't answering at {url}: start Zotero 7 and switch on Settings → "
               "Advanced → \"Allow other applications on this computer to communicate with "
               "Zotero\"")  # fmt: skip


class ZoteroError(Exception):
    pass


class Zotero:
    def __init__(self, url: str = DEFAULT_URL, *, storage: Path | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:  # fmt: skip
        host = urlsplit(url).hostname or ""
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ZoteroError("only Zotero's local API on this machine is supported")
        self.url = url.rstrip("/")
        self.storage = storage or Path.home() / "Zotero" / "storage"
        self._client = httpx.Client(transport=transport, trust_env=False, timeout=30,
                                    follow_redirects=False,
                                    headers={"Zotero-API-Version": "3"})  # fmt: skip

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: Any) -> httpx.Response:
        try:
            resp = self._client.get(f"{self.url}/users/0{path}", params=params)
        except httpx.HTTPError:
            raise ZoteroError(NOT_RUNNING.format(url=self.url)) from None
        if resp.status_code in (403, 404) and not path.endswith("/file"):
            raise ZoteroError(f"Zotero answered {resp.status_code} for {path}: is the local API "
                              "switched on?")  # fmt: skip
        return resp

    def _all(self, path: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            resp = self._get(path, limit=PAGE, start=start, **params)
            if resp.status_code != 200:
                raise ZoteroError(f"Zotero answered {resp.status_code} for {path}")
            page = resp.json()
            if not isinstance(page, list):
                raise ZoteroError(f"unexpected answer from Zotero for {path}")
            out += [p for p in page if isinstance(p, dict)]
            total = int(resp.headers.get("Total-Results", len(out)) or len(out))
            start += len(page)  # the server may send fewer than asked for
            if not page or start >= total or start > 100_000:
                return out

    def collections(self) -> list[dict[str, str]]:
        rows = self._all("/collections")
        by_key = {r["key"]: r for r in rows if "key" in r}

        def path(key: str, seen: int = 0) -> str:
            data = by_key[key].get("data", {})
            parent = data.get("parentCollection")
            name = str(data.get("name", key))
            if parent and parent in by_key and seen < 20:
                return f"{path(parent, seen + 1)}/{name}"
            return name

        return [{"key": k, "name": path(k)} for k in by_key]

    def find_collection(self, name: str) -> str:
        wanted = name.strip().strip("/").lower()
        matches = [
            c
            for c in self.collections()
            if c["name"].lower() == wanted or c["name"].lower().endswith("/" + wanted)
        ]
        if not matches:
            raise ZoteroError(f"no Zotero collection '{name}'")
        if len(matches) > 1:
            raise ZoteroError(f"'{name}' is ambiguous: " + ", ".join(m["name"] for m in matches))
        return matches[0]["key"]

    def items(self, collection: str | None) -> list[dict[str, Any]]:
        where = f"/collections/{collection}/items/top" if collection else "/items/top"
        return [i for i in self._all(where, format="json", include="data,csljson")
                if i.get("data", {}).get("itemType") not in ("attachment", "note")]  # fmt: skip

    def pdf(self, item_key: str) -> Path | None:
        """The local file of the item's first PDF attachment."""
        for att in self._all(f"/items/{item_key}/children", format="json"):
            data = att.get("data", {})
            if data.get("itemType") != "attachment" or data.get("contentType") != "application/pdf":
                continue
            found = self._attachment_file(att)
            if found is not None and found.is_file():
                return found
        return None

    def _attachment_file(self, att: dict[str, Any]) -> Path | None:
        href = ((att.get("links") or {}).get("enclosure") or {}).get("href", "")
        if href.startswith("file://"):
            return Path(unquote(urlsplit(href).path))
        data = att.get("data", {})
        stored = str(data.get("path") or "")
        if stored.startswith("storage:"):
            return self.storage / str(att.get("key", "")) / stored.split(":", 1)[1]
        if stored and Path(stored).is_absolute():  # a linked file
            return Path(stored)
        resp = self._get(f"/items/{att.get('key', '')}/file")  # the local API redirects
        location = resp.headers.get("location", "")
        if location.startswith("file://"):
            return Path(unquote(urlsplit(location).path))
        return None


def _slug(csl: dict[str, Any], key: str) -> str:
    authors = csl.get("author") or [{}]
    first = str(authors[0].get("family") or authors[0].get("literal") or "anon").lower()
    year = ((csl.get("issued") or {}).get("date-parts") or [[None]])[0][0] or "nd"
    words = re.findall(r"[a-z0-9]+", str(csl.get("title", "")).lower())[:4]
    base = "-".join([re.sub(r"[^a-z0-9]", "", first) or "anon", str(year), *words])
    return f"{base[:70]}-{key.lower()}"


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def import_items(zotero: Zotero, folder: Path, collection: str | None) -> dict[str, Any]:
    """Copy PDFs and metadata of a collection (or the whole library) into `folder/zotero`."""
    key = zotero.find_collection(collection) if collection else None
    target = folder / "zotero"
    report: dict[str, Any] = {"items": 0, "copied": 0, "unchanged": 0, "no_pdf": []}
    for item in zotero.items(key):
        report["items"] += 1
        item_key = str(item.get("key", ""))
        csl = item.get("csljson") or {}
        title = str(csl.get("title") or item.get("data", {}).get("title") or item_key)
        pdf = zotero.pdf(item_key)
        if pdf is None:
            report["no_pdf"].append(title[:80])
            continue
        target.mkdir(parents=True, exist_ok=True)
        dest = target / f"{_slug(csl, item_key)}.pdf"
        if (
            dest.exists()
            and dest.stat().st_size == pdf.stat().st_size
            and _digest(dest) == _digest(pdf)
        ):
            report["unchanged"] += 1
        else:
            shutil.copyfile(pdf, dest)
            report["copied"] += 1
        sidecar = dest.with_name(dest.name + ".csl.json")
        sidecar.write_text(json.dumps({**csl, "id": item_key}, indent=1, ensure_ascii=False),
                           encoding="utf-8")  # fmt: skip
    return report
