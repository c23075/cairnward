"""Docling as an optional PDF text extractor (D68): body text in reading order, tables as
Markdown, OCR for scanned PDFs. Per library: `cairnward library set <name> --parser
docling`; pypdfium2 stays the default and the fallback for any PDF Docling can't read.

Installed into its own virtual environment in the data folder (Cairnward's core never
imports it): `cairnward research engine install docling` resolves Docling (pinned below)
with CPU-only PyTorch, records the exact versions and hashes it got in
`requirements.lock.txt` (trust on first use: the repository can't ship that lock yet,
see docs), installs from that lock with hashes required, and downloads Docling's models
once. After that the worker runs with no network at all, sandboxed when the library's
environment has isolation on.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cairnward.config.loader import Config
from cairnward.config.paths import Paths

PIN = "docling==2.140.0"
WORKER = Path(__file__).with_name("docling_worker.py")


class DoclingError(Exception):
    pass


def engine_dir(paths: Paths) -> Path:
    return paths.data_dir / "engines" / "docling"


def python(paths: Paths) -> Path | None:
    exe = engine_dir(paths) / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.exists() and (engine_dir(paths) / "models").is_dir() else None


def install(paths: Paths, log: Any = print) -> dict[str, Any]:
    uv = shutil.which("uv")
    if uv is None:
        raise DoclingError("installing Docling needs uv (https://docs.astral.sh/uv/)")
    root = engine_dir(paths)
    root.mkdir(parents=True, exist_ok=True)
    venv, lock, models = root / "venv", root / "requirements.lock.txt", root / "models"
    log(f"resolving {PIN} with CPU-only PyTorch ...")
    subprocess.run([uv, "pip", "compile", "-", "--quiet", "--generate-hashes",
                    "--torch-backend", "cpu", "--python-version", "3.12", "-o", str(lock)],
                   input=PIN + "\n", text=True, check=True)  # fmt: skip
    log("installing (about 2 GB) ...")
    subprocess.run([uv, "venv", "--quiet", "--python", "3.12", str(venv)], check=True)
    subprocess.run([uv, "pip", "install", "--quiet", "--require-hashes", "--torch-backend", "cpu",
                    "-r", str(lock), "--python", str(venv)], check=True)  # fmt: skip
    tools = venv / ("Scripts/docling-tools.exe" if os.name == "nt" else "bin/docling-tools")
    log("downloading Docling's models (layout, tables, OCR) ...")
    subprocess.run([str(tools), "models", "download", "-o", str(models)], check=True)
    record = {"pin": PIN, "installed": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
              "models_sha256": tree_digest(models)}  # fmt: skip
    (root / "install.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def tree_digest(folder: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        h.update(f.relative_to(folder).as_posix().encode())
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def verify(paths: Paths) -> list[str]:
    """Changes since install: the lock or the models differ from what was recorded."""
    root = engine_dir(paths)
    try:
        record = json.loads((root / "install.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["no install record (reinstall: cairnward research engine install docling)"]
    problems = []
    lock = root / "requirements.lock.txt"
    if not lock.exists() or hashlib.sha256(lock.read_bytes()).hexdigest() != record["lock_sha256"]:
        problems.append("requirements.lock.txt changed since install")
    if tree_digest(root / "models") != record["models_sha256"]:
        problems.append("the models changed since install")
    return problems


Parsed = dict[str, Any]  # {"file", "pages": [...], "title"} or {"file", "error"}


def run_worker(cfg: Config, environment: str, files: list[Path], *, sandbox: bool | None = None,
               log: Callable[[str], None] = lambda m: None) -> list[Parsed]:  # fmt: skip
    """Convert `files` in one worker run (models load once). Sandboxed, with no network,
    when the environment has isolation on (or `sandbox` says so)."""
    from cairnward.sandbox import SandboxSpec, SandboxUnavailable, require, secret_dirs, wrap
    from cairnward.sandbox.srt import install_dir

    exe = python(cfg.paths)
    if exe is None:
        raise DoclingError("Docling isn't installed: cairnward research engine install docling")
    want_sandbox = (cfg.privacy(environment).value("isolation") == "on"
                    if sandbox is None else sandbox)  # fmt: skip
    root = engine_dir(cfg.paths)
    from cairnward.research.engines.paperqa import cache_dir

    work_base = cache_dir().parent / "docling"
    work_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="w-", dir=work_base) as work_s:
        work = Path(work_s)
        (work / "tmp").mkdir()
        spec = {"files": [str(f) for f in files], "models": str(root / "models"),
                "out": str(work / "out"), "ocr": True}  # fmt: skip
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
        env.update(HOME=str(work), TMPDIR=str(work / "tmp"))
        command = [str(exe), "-I", str(WORKER)]
        if want_sandbox:
            try:
                srt = require(cfg.paths)
            except SandboxUnavailable as exc:
                raise DoclingError(
                    f"isolation = on but the sandbox isn't available: {exc}"
                ) from None
            p = cfg.paths
            box = SandboxSpec(allowed_hosts=[], allow_write=[work],
                              deny_read=[*secret_dirs(), p.config_dir, p.data_dir, p.state_dir],
                              allow_read=(install_dir(p), root))  # fmt: skip
            with tempfile.TemporaryDirectory(prefix="cw-dl-") as tmp:
                argv = wrap(srt, box.write(Path(tmp) / "srt.json"), command, tmp_dir=work / "tmp")
                res = subprocess.run(argv, input=json.dumps(spec), capture_output=True, text=True,
                                     env={**env, "TMPDIR": tmp}, cwd=work, timeout=6 * 3600,
                                     check=False)  # fmt: skip
        else:
            log("cairnward: isolation = off: Docling runs unsandboxed")
            res = subprocess.run(command, input=json.dumps(spec), capture_output=True, text=True,
                                 env=env, cwd=work, timeout=6 * 3600, check=False)  # fmt: skip
        if not res.stdout.strip().splitlines()[-1:] or not res.stdout.strip().splitlines()[
                -1].startswith("DONE"):  # fmt: skip
            raise DoclingError(f"the Docling worker failed: {(res.stdout + res.stderr)[-500:]}")
        out: list[Parsed] = []
        for n, f in enumerate(files):
            try:
                out.append(json.loads((work / "out" / f"{n}.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                out.append({"file": str(f), "error": "no result"})
        return out
