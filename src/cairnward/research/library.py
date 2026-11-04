"""Literature libraries: folders of PDFs, indexed for page-cited answers (spec §4.6, FR-RES-1).

Indexing is resumable: each document is committed on its own, and documents whose file
hash hasn't changed are skipped. Search combines vector similarity (sqlite-vec) with
keyword ranking (FTS5 BM25) by reciprocal rank fusion, so exact terms (gene names,
acronyms) are found even when embeddings miss them.

Privacy: chunk text is stored as plain text in research.db (data folder, owner-only), unless
the library is locked with a passphrase (research/lock.py).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import sqlite_vec
from sqlalchemy import Connection, Engine, delete, insert, select, text, update

from cairnward.config.models import _check_name
from cairnward.config.paths import Paths
from cairnward.research.chunking import chunk_pages
from cairnward.research.embedder import Embedder
from cairnward.research.pdf import PdfError, PdfText, read_pdf
from cairnward.store.db import make_engine, upgrade
from cairnward.store.schema import chunks, documents, libraries

RRF_K = 60


class LibraryError(Exception):
    pass


class LibraryLockedError(LibraryError):
    pass


@dataclass(frozen=True)
class Library:
    id: int
    name: str
    folder: Path
    environment: str
    embedding_model: str | None
    embedding_dim: int | None
    locked: bool = False


@dataclass
class IndexReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    no_text: int = 0
    failed: int = 0
    missing: int = 0
    chunks: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    document_id: int
    title: str | None
    path: str
    page_start: int
    page_end: int
    text: str
    score: float
    vector_rank: int | None
    keyword_rank: int | None

    @property
    def pages(self) -> str:
        return (
            f"p. {self.page_start}"
            if self.page_start == self.page_end
            else (f"pp. {self.page_start}-{self.page_end}")
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fts_query(query: str) -> str | None:
    """User text -> a safe FTS5 query: quoted terms joined with OR."""
    terms = re.findall(r"[\w][\w\-']*", query.lower())
    terms = [t for t in terms if len(t) > 1][:32]
    return " OR ".join(f'"{t}"' for t in terms) or None


class Research:
    def __init__(self, engine: Engine, data_dir: Path | None = None):
        self._db = engine
        self.data_dir = data_dir

    @classmethod
    def open(cls, paths: Paths) -> Self:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        paths.data_dir.chmod(0o700)
        engine = make_engine(paths.data_dir / "research.db", vectors=True)
        upgrade(engine, "research")
        return cls(engine, paths.data_dir)

    def close(self) -> None:
        self._db.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ libraries

    @staticmethod
    def _lib(row: Any) -> Library:
        return Library(
            row.id,
            row.name,
            Path(row.folder),
            row.environment,
            row.embedding_model,
            row.embedding_dim,
        )

    def create(self, name: str, folder: Path, environment: str) -> Library:
        _check_name(name, "library name")
        folder = folder.expanduser().resolve()
        if not folder.is_dir():
            raise LibraryError(f"folder not found: {folder}")
        with self._db.begin() as conn:
            if conn.execute(select(libraries.c.id).where(libraries.c.name == name)).first():
                raise LibraryError(f"library '{name}' already exists")
            conn.execute(
                insert(libraries).values(
                    name=name, folder=str(folder), environment=environment, created_at=_now()
                )
            )
        return self.get(name)

    def locked_path(self, name: str) -> Path:
        if self.data_dir is None:
            raise LibraryError("this store has no data folder")
        return self.data_dir / "library-locks" / f"{name}.age"

    def _is_locked(self, name: str) -> bool:
        return self.data_dir is not None and self.locked_path(name).exists()

    def find(self, name: str) -> Library:
        """A library, locked or not; `get` is for anything that reads its contents."""
        with self._db.connect() as conn:
            row = conn.execute(select(libraries).where(libraries.c.name == name)).first()
        if row is None:
            raise LibraryError(f"no library named '{name}'")
        return dataclasses.replace(self._lib(row), locked=self._is_locked(name))

    def get(self, name: str) -> Library:
        lib = self.find(name)
        if lib.locked:
            raise LibraryLockedError(f"library '{name}' is locked: cairnward library unlock {name}")
        return lib

    def list_libraries(self) -> list[tuple[Library, int, int]]:
        """(library, documents, chunks)"""
        out = []
        with self._db.connect() as conn:
            for row in conn.execute(select(libraries).order_by(libraries.c.name)):
                n_docs = conn.execute(
                    text("SELECT count(*) FROM documents WHERE library_id = :l"), {"l": row.id}
                ).scalar_one()
                n_chunks = conn.execute(
                    text("SELECT coalesce(sum(chunks), 0) FROM documents WHERE library_id = :l"),
                    {"l": row.id},
                ).scalar_one()
                lib = dataclasses.replace(self._lib(row), locked=self._is_locked(row.name))
                out.append((lib, int(n_docs), int(n_chunks)))
        return out

    def remove(self, name: str) -> None:
        lib = self.find(name)
        with self._db.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {self._vec_table(lib)}"))
            conn.execute(delete(libraries).where(libraries.c.id == lib.id))
        if lib.locked:
            self.locked_path(name).unlink()
        with self._db.connect() as conn:
            conn.execute(text("VACUUM"))

    # ------------------------------------------------------------------ locking

    def export_rows(self, lib: Library) -> dict[str, Any]:
        """Everything research.db holds about a library's contents, for research/lock.py."""
        with self._db.connect() as conn:
            docs = conn.execute(select(documents).where(documents.c.library_id == lib.id)).all()
            ids = [d.id for d in docs]
            rows = conn.execute(select(chunks).where(chunks.c.document_id.in_(ids))).all()
            vectors = []
            if lib.embedding_dim:
                wanted = {r.id for r in rows}
                vectors = [
                    [r.chunk_id, base64.b64encode(bytes(r.embedding)).decode()]
                    for r in conn.execute(
                        text(f"SELECT chunk_id, embedding FROM {self._vec_table(lib)}")
                    )
                    if r.chunk_id in wanted
                ]
        return {
            "documents": [dict(d._mapping) for d in docs],
            "chunks": [dict(r._mapping) for r in rows],
            "vectors": vectors,
        }

    def wipe_rows(self, lib: Library) -> None:
        """Delete a library's contents, including from the keyword index and the file itself:
        external-content FTS5 keeps its tokens in its own tables until rebuilt."""
        with self._db.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {self._vec_table(lib)}"))
            conn.execute(delete(documents).where(documents.c.library_id == lib.id))
            conn.execute(text("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')"))
        with self._db.connect() as conn:
            conn.execute(text("VACUUM"))
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    def import_rows(self, lib: Library, data: dict[str, Any]) -> None:
        """Put back what `export_rows` saved, with new ids: another library may have taken
        the old ones. Replaces anything left by a crash while locking."""
        with self._db.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {self._vec_table(lib)}"))
            conn.execute(delete(documents).where(documents.c.library_id == lib.id))
            if lib.embedding_dim:
                conn.execute(text(
                    f"CREATE VIRTUAL TABLE {self._vec_table(lib)} USING vec0(chunk_id integer "
                    f"primary key, embedding float[{int(lib.embedding_dim)}] "
                    "distance_metric=cosine)"
                ))  # fmt: skip
            new_doc: dict[int, int] = {}
            for d in data["documents"]:
                old = d.pop("id")
                new_doc[old] = int(conn.execute(insert(documents).values(**d))
                                   .inserted_primary_key[0])  # type: ignore[index]  # fmt: skip
            new_chunk: dict[int, int] = {}
            for c in data["chunks"]:
                old = c.pop("id")
                c["document_id"] = new_doc[c["document_id"]]
                new_chunk[old] = int(conn.execute(insert(chunks).values(**c))
                                     .inserted_primary_key[0])  # type: ignore[index]  # fmt: skip
            for chunk_id, vector in data["vectors"]:
                conn.execute(
                    text(f"INSERT INTO {self._vec_table(lib)}(chunk_id, embedding) "
                         "VALUES (:c, :e)"),
                    {"c": new_chunk[chunk_id], "e": base64.b64decode(vector)},
                )  # fmt: skip

    def documents(self, name: str) -> list[Any]:
        lib = self.get(name)
        with self._db.connect() as conn:
            return list(
                conn.execute(
                    select(documents)
                    .where(documents.c.library_id == lib.id)
                    .order_by(documents.c.path)
                )
            )

    def chunk_rows(self, name: str, document_ids: list[int] | None = None) -> list[Any]:
        """(document_id, title, path, page_start, page_end, text) of a library's searchable
        chunks, in reading order (for quotation checks)."""
        lib = self.get(name)
        query = (
            select(chunks.c.document_id, documents.c.title, documents.c.path,
                   chunks.c.page_start, chunks.c.page_end, chunks.c.text)
            .join(documents, documents.c.id == chunks.c.document_id)
            .where(documents.c.library_id == lib.id, documents.c.status == "indexed")
            .order_by(chunks.c.document_id, chunks.c.seq)
        )  # fmt: skip
        if document_ids is not None:
            query = query.where(chunks.c.document_id.in_(document_ids))
        with self._db.connect() as conn:
            return list(conn.execute(query))

    # ------------------------------------------------------------------ vectors

    @staticmethod
    def _vec_table(lib: Library) -> str:
        return f"vec_{int(lib.id)}"

    def _ensure_vectors(self, conn: Connection, lib: Library, model: str, dim: int) -> None:
        if lib.embedding_dim is None:
            conn.execute(
                text(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._vec_table(lib)} USING "
                    f"vec0(chunk_id integer primary key, embedding float[{int(dim)}] "
                    "distance_metric=cosine)"
                )
            )
            conn.execute(
                update(libraries)
                .where(libraries.c.id == lib.id)
                .values(embedding_model=model, embedding_dim=dim)
            )
        elif lib.embedding_dim != dim or lib.embedding_model != model:
            raise LibraryError(
                f"library '{lib.name}' was indexed with {lib.embedding_model} ({lib.embedding_dim} "
                f"dimensions); now {model} ({dim}). Re-index from scratch with --rebuild."
            )

    # ------------------------------------------------------------------ indexing

    async def index(
        self,
        name: str,
        embedder: Embedder,
        *,
        prune: bool = False,
        rebuild: bool = False,
        only: list[str] | None = None,
        progress: Callable[[str], None] | None = None,
        target_tokens: int = 350,
        overlap_tokens: int = 60,
        parse_many: Callable[[list[Path]], Awaitable[dict[str, PdfText]]] | None = None,
        enrich: Callable[[Path, str], Awaitable[None]] | None = None,
    ) -> IndexReport:
        """Index new and changed PDFs. `parse_many` (Docling) reads the files that need
        reading in one go; any it can't read fall back to pypdfium2. `enrich` (GROBID) is
        called for each newly indexed file; its failures are reported, not fatal."""
        say = progress or (lambda _m: None)
        lib = self.get(name)
        if rebuild:
            with self._db.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {self._vec_table(lib)}"))
                conn.execute(delete(documents).where(documents.c.library_id == lib.id))
                conn.execute(
                    update(libraries)
                    .where(libraries.c.id == lib.id)
                    .values(embedding_model=None, embedding_dim=None)
                )
            lib = self.get(name)
        report = IndexReport()
        started = time.monotonic()
        known = {r.path: r for r in self.documents(name)}
        if only is not None:  # just these files (paths relative to the folder)
            files = sorted(p for p in (lib.folder / rel for rel in only) if p.is_file())
        else:
            files = sorted(
                p for p in lib.folder.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file()
            )
        seen: set[str] = set()
        digests = {path: sha256_file(path) for path in files}
        pre: dict[str, PdfText] = {}
        if parse_many is not None:
            todo = []
            for path in files:
                row = known.get(path.relative_to(lib.folder).as_posix())
                if row is None or row.sha256 != digests[path] or row.status not in (
                        "indexed", "no_text", "missing"):  # fmt: skip
                    todo.append(path)
            if todo:
                say(f"reading {len(todo)} PDF(s) with the richer parser ...")
                try:
                    pre = await parse_many(todo)
                except Exception as exc:  # the default parser still works
                    report.errors.append(f"richer parser: {exc}")
                    say(f"richer parser failed ({exc}); using the default for these")
        for n, path in enumerate(files, start=1):
            rel = path.relative_to(lib.folder).as_posix()
            seen.add(rel)
            digest = digests[path]
            old = known.get(rel)
            if old is not None and old.sha256 == digest and old.status in ("indexed", "no_text"):
                report.unchanged += 1
                continue
            if old is not None and old.sha256 == digest and old.status == "missing" and old.chunks:
                with self._db.begin() as conn:  # back, unchanged: no need to re-embed
                    conn.execute(
                        update(documents).where(documents.c.id == old.id).values(status="indexed")
                    )
                report.unchanged += 1
                continue
            t0 = time.monotonic()
            try:
                parsed = pre.get(str(path)) or read_pdf(path)
            except PdfError as exc:
                self._store_doc(lib, rel, digest, status="failed", error=str(exc))
                report.failed += 1
                report.errors.append(f"{rel}: {exc}")
                say(f"[{n}/{len(files)}] {rel}: FAILED ({exc})")
                continue
            if not parsed.has_text:
                self._store_doc(
                    lib,
                    rel,
                    digest,
                    status="no_text",
                    pages=len(parsed.pages),
                    title=parsed.title,
                    error="no extractable text (scanned? OCR is planned)",
                )
                report.no_text += 1
                say(f"[{n}/{len(files)}] {rel}: no text (scanned PDF?)")
                continue
            pieces = chunk_pages(
                parsed.pages, target_tokens=target_tokens, overlap_tokens=overlap_tokens
            )
            vectors = await embedder.embed([c.text for c in pieces])
            with self._db.begin() as conn:
                lib = self.get(name)
                self._ensure_vectors(conn, lib, embedder.model_name, len(vectors[0]))
                lib = Library(
                    lib.id,
                    lib.name,
                    lib.folder,
                    lib.environment,
                    embedder.model_name,
                    len(vectors[0]),
                )
                doc_id = self._replace_doc(conn, lib, rel, digest, parsed, len(pieces))
                vec = self._vec_table(lib)
                for piece, vector in zip(pieces, vectors, strict=True):
                    chunk_id = conn.execute(
                        insert(chunks).values(
                            document_id=doc_id,
                            seq=piece.seq,
                            page_start=piece.page_start,
                            page_end=piece.page_end,
                            text=piece.text,
                            tokens=piece.tokens,
                        )
                    ).inserted_primary_key[0]  # type: ignore[index]
                    conn.execute(
                        text(f"INSERT INTO {vec}(chunk_id, embedding) VALUES (:i, :e)"),
                        {"i": chunk_id, "e": sqlite_vec.serialize_float32(vector)},
                    )
            report.chunks += len(pieces)
            if old is None:
                report.added += 1
            else:
                report.updated += 1
            if enrich is not None:
                try:
                    await enrich(path, rel)
                except Exception as exc:  # metadata is extra; the text is indexed anyway
                    report.errors.append(f"{rel}: metadata: {exc}")
                    say(f"[{n}/{len(files)}] {rel}: metadata not extracted ({exc})")
            say(
                f"[{n}/{len(files)}] {rel}: {len(parsed.pages)} pages, {len(pieces)} chunks "
                f"({time.monotonic() - t0:.1f}s)"
            )
        for rel, row in known.items():
            if rel in seen or (only is not None and rel not in only):
                continue
            report.missing += 1
            with self._db.begin() as conn:
                if prune:
                    self._delete_doc(conn, lib, row.id)
                else:
                    conn.execute(
                        update(documents).where(documents.c.id == row.id).values(status="missing")
                    )
        report.seconds = round(time.monotonic() - started, 2)
        return report

    def _delete_doc(self, conn: Connection, lib: Library, doc_id: int) -> None:
        ids = [
            r.id for r in conn.execute(select(chunks.c.id).where(chunks.c.document_id == doc_id))
        ]
        if ids and lib.embedding_dim:
            conn.execute(
                text(
                    f"DELETE FROM {self._vec_table(lib)} WHERE chunk_id IN "
                    f"({','.join(str(int(i)) for i in ids)})"
                )
            )
        conn.execute(delete(documents).where(documents.c.id == doc_id))  # chunks cascade

    def _replace_doc(
        self, conn: Connection, lib: Library, rel: str, digest: str, parsed: Any, n_chunks: int
    ) -> int:
        old = conn.execute(
            select(documents.c.id).where(documents.c.library_id == lib.id, documents.c.path == rel)
        ).first()
        if old:
            self._delete_doc(conn, lib, old.id)
        result = conn.execute(
            insert(documents).values(
                library_id=lib.id,
                path=rel,
                sha256=digest,
                title=parsed.title,
                authors=parsed.authors,
                year=parsed.year,
                pages=len(parsed.pages),
                status="indexed",
                chunks=n_chunks,
                indexed_at=_now(),
            )
        )
        return int(result.inserted_primary_key[0])  # type: ignore[index]

    def _store_doc(
        self,
        lib: Library,
        rel: str,
        digest: str,
        *,
        status: str,
        error: str,
        pages: int | None = None,
        title: str | None = None,
    ) -> None:
        with self._db.begin() as conn:
            old = conn.execute(
                select(documents.c.id).where(
                    documents.c.library_id == lib.id, documents.c.path == rel
                )
            ).first()
            if old:
                self._delete_doc(conn, lib, old.id)
            conn.execute(
                insert(documents).values(
                    library_id=lib.id,
                    path=rel,
                    sha256=digest,
                    title=title,
                    pages=pages,
                    status=status,
                    error=error,
                    chunks=0,
                    indexed_at=_now(),
                )
            )

    # ------------------------------------------------------------------ search

    async def search(self, name: str, query: str, embedder: Embedder, *, k: int = 8) -> list[Hit]:
        lib = self.get(name)
        if lib.embedding_dim is None:
            raise LibraryError(f"library '{name}' isn't indexed yet")
        if embedder.model_name != lib.embedding_model:
            raise LibraryError(
                f"library '{name}' was indexed with {lib.embedding_model}; "
                f"search with the same model"
            )
        (qvec,) = await embedder.embed([query])
        pool = max(k * 4, 20)
        with self._db.connect() as conn:
            vec_ranked = [
                r.chunk_id
                for r in conn.execute(
                    text(
                        f"SELECT chunk_id FROM {self._vec_table(lib)} "
                        "WHERE embedding MATCH :q AND k = :k "
                        "ORDER BY distance"
                    ),
                    {"q": sqlite_vec.serialize_float32(qvec), "k": pool},
                )
            ]
            kw_ranked: list[int] = []
            if fq := fts_query(query):
                kw_ranked = [
                    r.id
                    for r in conn.execute(
                        text(
                            "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                            "JOIN documents d ON d.id = c.document_id "
                            "WHERE chunks_fts MATCH :q AND d.library_id = :l "
                            "AND d.status = 'indexed' "
                            "ORDER BY bm25(chunks_fts) LIMIT :n"
                        ),
                        {"q": fq, "l": lib.id, "n": pool},
                    )
                ]
            if vec_ranked:  # only papers that are still available
                ok = {
                    r.id
                    for r in conn.execute(
                        select(chunks.c.id)
                        .join(documents, documents.c.id == chunks.c.document_id)
                        .where(chunks.c.id.in_(vec_ranked), documents.c.status == "indexed")
                    )
                }
                vec_ranked = [c for c in vec_ranked if c in ok]
            scores: dict[int, float] = {}
            vrank = {cid: i + 1 for i, cid in enumerate(vec_ranked)}
            krank = {cid: i + 1 for i, cid in enumerate(kw_ranked)}
            for cid in set(vrank) | set(krank):
                scores[cid] = sum(1 / (RRF_K + r[cid]) for r in (vrank, krank) if cid in r)
            top = sorted(scores, key=lambda c: -scores[c])[:k]
            if not top:
                return []
            rows = {
                r.id: r
                for r in conn.execute(
                    select(
                        chunks.c.id,
                        chunks.c.document_id,
                        chunks.c.page_start,
                        chunks.c.page_end,
                        chunks.c.text,
                        documents.c.title,
                        documents.c.path,
                    )
                    .join(documents, documents.c.id == chunks.c.document_id)
                    .where(chunks.c.id.in_(top), documents.c.status == "indexed")
                )
            }
        return [
            Hit(
                cid,
                rows[cid].document_id,
                rows[cid].title,
                rows[cid].path,
                rows[cid].page_start,
                rows[cid].page_end,
                rows[cid].text,
                round(scores[cid], 5),
                vrank.get(cid),
                krank.get(cid),
            )
            for cid in top
            if cid in rows
        ]
