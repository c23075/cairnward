"""Presidio as an optional personal-data detector for the redaction scanner (FR-RED-2):
names, places and groups that patterns can't find. `[redaction] presidio = true` adds its
findings (category "personal") to the regex scanner's.

Installed into its own virtual environment (`cairnward redaction install presidio`):
presidio-analyzer (pinned below, MIT) and spaCy's small English model, from the lock
shipped in engines/locks/presidio.txt (every package and the model wheel hash-pinned).
The worker is a long-running child process (the model loads once), started on first use
by whichever process scans: inside the core's sandbox when the core is isolated; it needs
no network. If it can't start or stops answering, the regex scanner still applies and the
report says Presidio was unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from cairnward.config.paths import Paths

PIN = "presidio-analyzer==2.2.364"
SPACY_MODEL = "en_core_web_sm"
# The model is a wheel in the lock (with its hash), not `spacy download` (no hash check).
SPACY_MODEL_URL = ("https://github.com/explosion/spacy-models/releases/download/"
                   "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl")  # fmt: skip
WORKER = Path(__file__).with_name("presidio_worker.py")
START_TIMEOUT_S = 120
REQUEST_TIMEOUT_S = 20


class PresidioError(Exception):
    pass


def engine_dir(paths: Paths) -> Path:
    return paths.data_dir / "engines" / "presidio"


def python(paths: Paths) -> Path | None:
    exe = engine_dir(paths) / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.exists() else None


def install(paths: Paths, log: Any = print) -> dict[str, Any]:
    uv = shutil.which("uv")
    if uv is None:
        raise PresidioError("installing Presidio needs uv (https://docs.astral.sh/uv/)")
    root = engine_dir(paths)
    root.mkdir(parents=True, exist_ok=True)
    venv, lock = root / "venv", root / "requirements.lock.txt"
    from cairnward.research.engines import locks

    ready = locks.shipped("presidio")
    if ready is not None:
        log(f"using the lock shipped with Cairnward ({ready.name})")
        shutil.copyfile(ready, lock)
    else:
        log(f"resolving {PIN} (recorded: trust on first use) ...")
        subprocess.run([uv, "pip", "compile", "-", "--quiet", "--generate-hashes",
                        "--python-version", "3.12", "-o", str(lock)],
                       input=f"{PIN}\n{SPACY_MODEL} @ {SPACY_MODEL_URL}\n", text=True,
                       check=True)  # fmt: skip
    subprocess.run([uv, "venv", "--quiet", "--python", "3.12", str(venv)], check=True)
    subprocess.run([uv, "pip", "install", "--quiet", "--require-hashes", "-r", str(lock),
                    "--python", str(venv)], check=True)  # fmt: skip
    if python(paths) is None:
        raise PresidioError("the virtual environment wasn't created")
    frozen = subprocess.run([uv, "pip", "freeze", "--python", str(venv)], capture_output=True,
                            text=True, check=True).stdout  # fmt: skip
    record = {"pin": PIN, "installed": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "lock_source": f"repository ({ready.name})" if ready else "resolved on this machine",
              "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
              "freeze_sha256": hashlib.sha256(frozen.encode()).hexdigest()}  # fmt: skip
    (root / "install.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


class Presidio:
    """One worker process, shared by the threads that scan (requests are serialised)."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._next = 0

    def _start(self) -> subprocess.Popen[str]:
        exe = python(self.paths)
        if exe is None:
            raise PresidioError("Presidio isn't installed: cairnward redaction install presidio")
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
        env.update(HOME=str(engine_dir(self.paths)), NO_PROXY="*")
        proc = subprocess.Popen([str(exe), "-I", str(WORKER), SPACY_MODEL], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                env=env, bufsize=1)  # fmt: skip
        line = self._readline(proc, START_TIMEOUT_S)
        if line.strip() != "READY":
            proc.kill()
            raise PresidioError(f"the Presidio worker didn't start ({line.strip()[:200]!r})")
        return proc

    @staticmethod
    def _readline(proc: subprocess.Popen[str], timeout: float) -> str:
        result: list[str] = []
        reader = threading.Thread(target=lambda: result.append(proc.stdout.readline()),  # type: ignore[union-attr]
                                  daemon=True)  # fmt: skip
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            proc.kill()
            raise PresidioError("the Presidio worker stopped answering")
        return result[0] if result else ""

    def analyze(self, text: str, entities: list[str], min_score: float) -> list[dict[str, Any]]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._start()
            self._next += 1
            req = {"id": self._next, "text": text, "entities": entities, "min_score": min_score}
            if self._proc.stdin is None:
                raise PresidioError("the Presidio worker has no input")
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._proc = None
                raise PresidioError("the Presidio worker stopped") from None
            line = self._readline(self._proc, REQUEST_TIMEOUT_S)
            try:
                reply = json.loads(line)
            except ValueError:
                self._proc = None
                raise PresidioError("the Presidio worker sent nonsense") from None
            if "error" in reply:
                raise PresidioError(reply["error"])
            findings: list[dict[str, Any]] = reply.get("findings", [])
            return findings

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                self._proc.kill()
                self._proc = None


_SHARED: dict[str, Presidio] = {}


def shared(paths: Paths) -> Presidio:
    key = str(paths.data_dir)
    if key not in _SHARED:
        _SHARED[key] = Presidio(paths)
    return _SHARED[key]
