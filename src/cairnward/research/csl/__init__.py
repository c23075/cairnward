"""CSL engines for reference formatting (FR-RES-6, D67 as revised 2026-11-02).

CSL (Citation Style Language) style files describe referencing styles; an engine applies
one to CSL-JSON references. Cairnward supports three engines, chosen with
`[research] citation_engine` in cairnward.toml (or `cairnward cite --engine`):

- `builtin`: Cairnward's own APA 7 and IEEE formatter; always available, those two only.
- `citeproc-js`: the reference CSL engine (Zotero's), run with Node under its permission
  model; installed with `cairnward research engine install citeproc-js`.
- `pandoc`: pandoc's citeproc, if pandoc (>= 2.11) is installed; run with `--sandbox`.
- `auto` (default): citeproc-js if installed, else pandoc, else builtin (for APA/IEEE).

The shipped APA and IEEE style files and the en-US locale are vendored unmodified from the
CSL project and checked against SHA-256 hashes in MANIFEST.json before use. Any other
`.csl` file can be given by path (needs citeproc-js or pandoc).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

from cairnward.config.paths import Paths

Engine = Literal["auto", "builtin", "citeproc-js", "pandoc"]
ENGINES: tuple[Engine, ...] = ("auto", "builtin", "citeproc-js", "pandoc")
HERE = Path(__file__).resolve().parent
SHIPPED = {"apa": "styles/apa.csl", "ieee": "styles/ieee.csl"}
LOCALE = "locales/locales-en-US.xml"
MIN_NODE = (20, 0)
TIMEOUT_S = 120


class CslError(Exception):
    pass


# ------------------------------------------------------------------ vendored files


def vendored(rel: str) -> Path:
    """A vendored style or locale, after checking its hash."""
    manifest = json.loads((HERE / "MANIFEST.json").read_text(encoding="utf-8"))
    expected = {**manifest["styles"]["files"], **manifest["locales"]["files"]}.get(rel)
    path = HERE / rel
    if expected is None or not path.is_file():
        raise CslError(f"{rel} isn't one of Cairnward's vendored CSL files")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise CslError(f"{rel} doesn't match its recorded SHA-256 (MANIFEST.json); reinstall")
    return path


def style_file(style: str) -> Path:
    """`apa`/`ieee` (vendored, verified) or a path to a .csl file."""
    if style.lower() in SHIPPED:
        return vendored(SHIPPED[style.lower()])
    path = Path(style).expanduser()
    if path.suffix.lower() != ".csl":
        raise CslError(f"unknown style '{style}': apa, ieee, or a .csl file")
    if not path.is_file():
        raise CslError(f"style file {path} not found")
    return path


# ------------------------------------------------------------------ engines available


def citeproc_js_dir(paths: Paths) -> Path:
    return paths.data_dir / "engines" / "citeproc-js"


def citeproc_js(paths: Paths) -> Path | None:
    lib = citeproc_js_dir(paths) / "node_modules" / "citeproc" / "citeproc_commonjs.js"
    return lib if lib.is_file() and node_version() else None


def node_version() -> tuple[int, int] | None:
    from cairnward.sandbox.srt import node_version as nv

    v = nv()
    return v if v and v >= MIN_NODE else None


def pandoc() -> str | None:
    exe = shutil.which("pandoc")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,  # noqa: S603
                             check=True).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"pandoc(?:\.exe)? (\d+)\.(\d+)", out)
    return exe if m and (int(m.group(1)), int(m.group(2))) >= (2, 11) else None


def available(paths: Paths) -> list[str]:
    found = ["builtin"]
    if citeproc_js(paths):
        found.append("citeproc-js")
    if pandoc():
        found.append("pandoc")
    return found


def choose(engine: str, style: str, paths: Paths) -> str:
    """The engine that will format `style`, or CslError saying what to install."""
    shipped = style.lower() in SHIPPED
    if not shipped and not style.lower().endswith(".csl"):
        raise CslError(f"unknown style '{style}': apa, ieee, or a .csl file")
    if engine == "auto":
        for candidate in ("citeproc-js", "pandoc"):
            if candidate in available(paths):
                return candidate
        if shipped:
            return "builtin"
        raise CslError("other .csl styles need citeproc-js (cairnward research engine install "
                       "citeproc-js) or pandoc")  # fmt: skip
    if engine == "builtin":
        if not shipped:
            raise CslError("the built-in formatter knows apa and ieee only; use citeproc-js or "
                           "pandoc for .csl styles")  # fmt: skip
        return engine
    if engine == "citeproc-js" and not citeproc_js(paths):
        raise CslError("citeproc-js isn't installed: cairnward research engine install "
                       "citeproc-js (needs Node.js >= 20)")  # fmt: skip
    if engine == "pandoc" and not pandoc():
        raise CslError("pandoc (>= 2.11) isn't installed")
    if engine not in ENGINES:
        raise CslError(f"unknown citation engine '{engine}': {', '.join(ENGINES)}")
    return engine


def install_citeproc_js(paths: Paths, log: Any = print) -> Path:
    """npm ci the pinned citeproc-js into the data folder (integrity hash from the lockfile;
    no install scripts run)."""
    if node_version() is None:
        raise CslError("Node.js >= 20 is required for citeproc-js")
    npm = shutil.which("npm")
    if not npm:
        raise CslError("npm not found")
    target = citeproc_js_dir(paths)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copy(HERE / "pkg" / name, target / name)
    log(f"installing citeproc-js (pinned) into {target} ...")
    subprocess.run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--omit=dev"],  # noqa: S603
                   cwd=target, check=True)  # fmt: skip
    lib = citeproc_js(paths)
    if lib is None:
        raise CslError("citeproc-js didn't install")
    return lib


# ------------------------------------------------------------------ running them


def prepare(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies with ids, and DOIs bare (CSL styles add the https://doi.org/ prefix)."""
    out = []
    for n, item in enumerate(items):
        it = dict(item)
        it["id"] = str(it.get("id") or f"item{n}")
        if isinstance(it.get("DOI"), str):
            it["DOI"] = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", it["DOI"].strip(),
                               flags=re.I)  # fmt: skip
        out.append(it)
    return out


def _node_permission_flags(root_dirs: list[Path]) -> list[str]:
    from cairnward.sandbox.srt import node_version as nv

    v = nv() or (0, 0)
    flag = "--permission" if v >= (23, 5) else "--experimental-permission"
    return [flag, *(f"--allow-fs-read={d}" for d in root_dirs)]


def run_citeproc_js(items: list[dict[str, Any]], csl: Path, paths: Paths) -> list[str]:
    lib = citeproc_js(paths)
    node = shutil.which("node")
    if lib is None or node is None:
        raise CslError("citeproc-js isn't installed")
    spec = {"citeproc": str(lib), "items": items, "style": csl.read_text(encoding="utf-8"),
            "locale": vendored(LOCALE).read_text(encoding="utf-8")}  # fmt: skip
    cmd = [node, *_node_permission_flags([HERE, citeproc_js_dir(paths)]), "--no-warnings",
           str(HERE / "format.mjs")]  # fmt: skip
    res = subprocess.run(cmd, input=json.dumps(spec), capture_output=True, text=True,  # noqa: S603
                         timeout=TIMEOUT_S, check=False, encoding="utf-8")  # fmt: skip
    if res.returncode != 0:
        raise CslError(f"citeproc-js failed: {res.stderr.strip()[-400:]}")
    out = json.loads(res.stdout)
    if out.get("errors"):
        raise CslError(f"citeproc-js: {out['errors'][:3]}")
    return list(out["entries"])


def run_pandoc(items: list[dict[str, Any]], csl: Path) -> list[str]:
    exe = pandoc()
    if exe is None:
        raise CslError("pandoc isn't installed")
    with tempfile.TemporaryDirectory(prefix="cw-csl-") as tmp:
        bib = Path(tmp) / "refs.json"
        bib.write_text(json.dumps(items), encoding="utf-8")
        locale = Path(tmp) / "locales-en-US.xml"
        shutil.copy(vendored(LOCALE), locale)
        cmd = [exe, "--sandbox", "-f", "markdown", "-t", "html", "--citeproc", "--csl", str(csl),
               "--bibliography", str(bib), "--metadata", "lang=en-US", "--wrap=none"]  # fmt: skip
        doc = "---\nnocite: '@*'\nsuppress-bibliography: false\n---\n"
        res = subprocess.run(cmd, input=doc, capture_output=True, text=True, timeout=TIMEOUT_S,  # noqa: S603
                             check=False, encoding="utf-8")  # fmt: skip
    if res.returncode != 0:
        raise CslError(f"pandoc failed: {res.stderr.strip()[-400:]}")
    return _entries(res.stdout)


# ------------------------------------------------------------------ HTML -> Markdown / text


class _Entries(HTMLParser):
    """Each `csl-entry` element as text; <i>/<em> -> *...* when md; a left-margin label
    (e.g. "[1]") followed by one space."""

    def __init__(self, md: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.md, self.depth = md, 0
        self.entries: list[str] = []
        self.cur: list[str] = []
        self.stack: list[tuple[str, bool]] = []  # (tag, closes a left margin)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = dict(attrs).get("class") or ""
        if "csl-entry" in cls.split():
            self.depth, self.cur = 1, []
            self.stack = [(tag, False)]
            return
        if not self.depth:
            return
        self.depth += 1
        self.stack.append((tag, "csl-left-margin" in cls.split()))
        if tag in ("i", "em") and self.md:
            self.cur.append("*")
        if tag == "br":  # void element
            self.depth -= 1
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return
        name, margin = self.stack.pop() if self.stack else (tag, False)
        self.depth -= 1
        if name in ("i", "em") and self.md:
            self.cur.append("*")
        if margin:
            self.cur.append(" ")
        if self.depth == 0:
            text = re.sub(r"\s+", " ", "".join(self.cur)).strip()
            self.entries.append(text.replace("* *", " ").replace("**", ""))

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.cur.append(data)


def _entries(markup: str) -> list[str]:
    """The raw HTML of each entry (for to_text)."""
    parser = _Raw()
    parser.feed(markup)
    return parser.entries


class _Raw(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.entries: list[str] = []
        self.depth = 0
        self.buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = (dict(attrs).get("class") or "").split()
        text = self.get_starttag_text() or ""
        if "csl-entry" in cls and not self.depth:
            self.depth, self.buf = 1, [text]
        elif self.depth:
            self.depth += 1
            self.buf.append(text)

    def handle_endtag(self, tag: str) -> None:
        if self.depth:
            self.buf.append(f"</{tag}>")
            self.depth -= 1
            if not self.depth:
                self.entries.append("".join(self.buf))

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.buf.append(data)

    def handle_entityref(self, name: str) -> None:
        if self.depth:
            self.buf.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.depth:
            self.buf.append(f"&#{name};")


def to_text(entry_html: str, *, md: bool) -> str:
    if "csl-entry" not in entry_html:
        entry_html = f'<div class="csl-entry">{entry_html}</div>'
    parser = _Entries(md)
    parser.feed(entry_html)
    parser.close()
    return (
        parser.entries[0] if parser.entries else html.unescape(re.sub(r"<[^>]+>", "", entry_html))
    )


def format_with(engine: str, items: list[dict[str, Any]], style: str, paths: Paths, *,
                md: bool) -> list[str]:  # fmt: skip
    """Format with a CSL engine (citeproc-js or pandoc)."""
    csl = style_file(style)
    prepared = prepare(items)
    raw = (run_citeproc_js(prepared, csl, paths) if engine == "citeproc-js"
           else run_pandoc(prepared, csl))  # fmt: skip
    return [to_text(e, md=md) for e in raw]
