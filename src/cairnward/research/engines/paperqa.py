"""PaperQA2 as an optional "deep" engine (ADR-0014, FR-RES-13).

- Installed on request into its own virtual environment in the data folder, from a
  hash-pinned requirements file (`cairnward research engine install paperqa`); none of
  its 78 dependencies enter Cairnward's core.
- Each question runs `paperqa_worker.py` with that environment's Python, like an agent
  run: a short-lived run key (budget, audit `run.start`/`run.end`), and, with isolation
  on, the sandbox with network access to Cairnward's gateway only. Every model call it
  makes is a gateway call: policy-checked, queued, audited.
- The worker refuses to start unless every model setting points at the gateway and
  online lookups and image enrichment are off (guard()).
- Its answer is mapped to Cairnward's Answer: PaperQA2's citation keys become [n] with
  file and pages; quotations are checked against the library like the lean pipeline's.

The worker keeps PaperQA2's index (its own chunks and embeddings) in
`$CAIRNWARD_CACHE_DIR/paperqa/<library>/` (default `~/.cache/cairnward`), outside the
data folder the sandbox hides; it is rebuilt when the models change.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnward.config.loader import Config
from cairnward.config.paths import Paths

HERE = Path(__file__).parent
WORKER = HERE / "paperqa_worker.py"
REQUIREMENTS = HERE / "paperqa-requirements.txt"
_NAME = re.compile(r"^(?P<doc>.+?) pages (?P<start>\d+)(?:-(?P<end>\d+))?$")


class EngineError(Exception):
    pass


def engine_dir(paths: Paths) -> Path:
    return paths.data_dir / "engines" / "paperqa"


def cache_dir() -> Path:
    """The engine's working data (its own index of your papers): outside Cairnward's
    data folder, which the sandbox hides. Owner-only."""
    base = os.environ.get("CAIRNWARD_CACHE_DIR") or str(
        Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "cairnward"
    )
    return Path(base) / "paperqa"  # fmt: skip


def cache_key(paths: Paths) -> bytes:
    """The key that signs the worker's pickled index (in the state folder, which the
    sandbox hides; owner-only). Created on first use."""
    path = paths.state_dir / "paperqa-cache.key"
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key.hex())
    tmp.replace(path)
    return key


def python(paths: Paths) -> Path | None:
    exe = engine_dir(paths) / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return exe if exe.exists() else None


def install(paths: Paths, log: Any = print) -> Path:
    """A fresh virtual environment with the pinned, hash-checked PaperQA2."""
    root = engine_dir(paths)
    root.mkdir(parents=True, exist_ok=True)
    venv = root / "venv"
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "venv", "--quiet", "--python", "3.12", str(venv)], check=True)
        subprocess.run([uv, "pip", "install", "--quiet", "--require-hashes", "-r",
                        str(REQUIREMENTS), "--python", str(venv)], check=True)  # fmt: skip
    else:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        exe = python(paths)
        if exe is None:
            raise EngineError("the virtual environment wasn't created")
        subprocess.run([str(exe), "-m", "pip", "install", "--quiet", "--require-hashes",
                        "-r", str(REQUIREMENTS)], check=True)  # fmt: skip
    exe = python(paths)
    if exe is None:
        raise EngineError("PaperQA2 didn't install")
    log(f"PaperQA2 installed in {venv}")
    return exe


def _slug(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "", text)[:24] or "paper"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}{n}", n + 1
    taken.add(name)
    return name


def to_answer(question: str, library: str, result: dict[str, Any],
              papers: dict[str, dict[str, Any]], rows: list[Any]) -> Any:  # fmt: skip
    """PaperQA2's answer as Cairnward's Answer: citation keys -> [n] with file and pages."""
    from cairnward.research.ask import Answer, Source
    from cairnward.research.check import _find, quotations

    text = str(result.get("answer") or "").strip()
    answer = Answer(question=question, library=library, text=text,
                    passages=len(result.get("contexts", [])))  # fmt: skip
    names = sorted(
        {c["name"] for c in result.get("contexts", []) if c.get("score", 0) > 0},
        key=len,
        reverse=True,
    )  # longest first, so "x pages 2-3" beats "x pages 2"
    found = sorted((text.find(n), n) for n in names if n in text)  # first mention first
    numbers = {name: i for i, (_pos, name) in enumerate(found, 1)}
    for name in names:  # longest first, so "x pages 2-3" isn't cut by "x pages 2"
        if name in numbers:
            text = text.replace(name, f"[{numbers[name]}]")
    for name, n in sorted(numbers.items(), key=lambda kv: kv[1]):
        m = _NAME.match(name)
        doc = papers.get(m.group("doc") if m else "", {})
        pages = (m.group("start") if not m.group("end") or m.group("end") == m.group("start")
                 else f"{m.group('start')}-{m.group('end')}") if m else "?"  # fmt: skip
        answer.sources.append(Source(n, doc.get("title", name), doc.get("file", name), pages,
                                     int(doc.get("document_id", 0)), 0))  # fmt: skip
    text = re.sub(
        r"\((\[\d+\](?:,\s*\[\d+\])*)\)",
        lambda mm: "[" + ", ".join(re.findall(r"\d+", mm.group(1))) + "]",
        text,
    )
    answer.text = text
    answer.cannot_answer = "cannot answer" in text.lower() or "can't answer" in text.lower()
    answer.uncited = not answer.sources
    for quote, _cited in quotations(text):
        if _find(quote, rows) is None:
            short = quote if len(quote) <= 80 else quote[:77] + "..."
            answer.quote_problems.append(f'"{short}" was not found in the papers')
    return answer


@dataclass
class Prepared:
    """What a question needs: the library's papers as PaperQA2 documents, its chunks
    (for quotation checks), the models, and the engine's working folder."""

    library: str
    environment: str
    papers: dict[str, dict[str, Any]]
    rows: list[Any]
    models: dict[str, str]
    work: Path


def prepare(cfg: Config, library: str, effort: str | None = None, *,
            create_work: bool = True) -> Prepared:  # fmt: skip
    """create_work=False: only what's needed to map an answer (the isolated core can't
    write the cache folder; the process that runs the worker prepares it)."""
    from cairnward.research import Research

    if python(cfg.paths) is None:
        raise EngineError("PaperQA2 isn't installed: cairnward research engine install paperqa")
    with Research.open(cfg.paths) as research:
        lib = research.get(library)
        docs = [d for d in research.documents(library) if d.status == "indexed"]
        rows = research.chunk_rows(library)
    taken: set[str] = set()
    papers: dict[str, dict[str, Any]] = {}
    for d in docs:
        name = _slug(Path(d.path).stem, taken)
        papers[name] = {"path": str(lib.folder / d.path), "docname": name,
                        "title": d.title or Path(d.path).stem, "file": Path(d.path).name,
                        "document_id": d.id}  # fmt: skip
    env_name = lib.environment
    env_cfg = cfg.environments[env_name]
    main_effort = effort or env_cfg.effort_default or cfg.main.defaults.effort
    models = {"main": f"{env_name}:{main_effort}", "fast": f"{env_name}:low",
              "embed": f"{env_name}:embed"}  # fmt: skip
    work = cache_dir() / re.sub(r"[^A-Za-z0-9._-]", "_", library)
    if not create_work:
        return Prepared(library, env_name, papers, rows, models, work)
    for d in (work, work / "home", work / "tmp"):
        d.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        for d in (cache_dir().parent, cache_dir(), work):
            d.chmod(0o700)
    meta = work / "models.json"
    if meta.exists() and json.loads(meta.read_text()) != models:
        for name in ("index.pkl", "index.pkl.sig"):  # embeddings from other models
            (work / name).unlink(missing_ok=True)
    meta.write_text(json.dumps(models))
    return Prepared(library, env_name, papers, rows, models, work)


def sandbox_command(cfg: Config, environment: str) -> list[str] | None:
    """srt when the environment wants isolation (None: run unsandboxed)."""
    from cairnward.sandbox import SandboxUnavailable, require

    if cfg.privacy(environment).value("isolation") != "on":
        return None
    try:
        return require(cfg.paths)
    except SandboxUnavailable as exc:
        raise EngineError(f"isolation = on but the sandbox isn't available: {exc}") from None


def execute(cfg: Config, prep: Prepared, question: str, *, gateway_port: int, key: str,
            evidence_k: int = 10, skip_summary: bool = False,
            log: Any = print) -> dict[str, Any]:  # fmt: skip
    """Run the worker once (sandboxed when the environment says so); its raw result.
    Called by the CLI process or by whichever server process may start sandboxes."""
    from cairnward.sandbox import SandboxSpec, secret_dirs, wrap
    from cairnward.sandbox.srt import install_dir

    exe = python(cfg.paths)
    if exe is None:
        raise EngineError("PaperQA2 isn't installed: cairnward research engine install paperqa")
    srt = sandbox_command(cfg, prep.environment)
    work = prep.work
    spec = {"gateway": f"http://127.0.0.1:{gateway_port}/v1", "models": prep.models,
            "papers": list(prep.papers.values()), "question": question,
            "cache": str(work / "index.pkl"), "cache_key": cache_key(cfg.paths).hex(),
            "evidence_k": evidence_k, "skip_summary": skip_summary}  # fmt: skip
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
    env.update(HOME=str(work / "home"), TMPDIR=str(work / "tmp"), CAIRNWARD_API_KEY=key,
               NO_PROXY="")  # fmt: skip
    command = [str(exe), "-I", str(WORKER)]
    if srt is not None:
        p = cfg.paths
        sandbox = SandboxSpec(
            allowed_hosts=[f"127.0.0.1:{gateway_port}"],
            allow_write=[work],
            deny_read=[*secret_dirs(), p.config_dir, p.data_dir, p.state_dir],
            allow_read=(install_dir(p), engine_dir(p)),
        )
        with tempfile.TemporaryDirectory(prefix="cairnward-pqa-") as tmp:
            settings = sandbox.write(Path(tmp) / "srt.json")
            # srt's own sockets need a short path; the worker gets work/tmp inside
            argv = wrap(srt, settings, command, tmp_dir=work / "tmp")
            code, out = _run(argv, spec, {**env, "TMPDIR": tmp}, work)
    else:
        log("cairnward: isolation = off: PaperQA2 runs unsandboxed")
        code, out = _run(command, spec, env, work)
    line = next((x for x in reversed(out.splitlines()) if x.startswith(("RESULT ", "ERROR "))), "")
    if not line.startswith("RESULT "):
        detail = json.loads(line[6:])["error"] if line.startswith("ERROR ") else (
            f"the worker stopped (exit {code}); see {work / 'worker.log'}")  # fmt: skip
        raise EngineError(detail)
    result: dict[str, Any] = json.loads(line[7:])
    return result


def finish(prep: Prepared, question: str, result: dict[str, Any], seconds: float) -> Any:
    answer = to_answer(question, prep.library, result, prep.papers, prep.rows)
    answer.seconds = seconds
    answer.model = f"paperqa ({prep.models['main']})"
    return answer


def audit_answer(audit: Any, prep: Prepared, question: str, answer: Any, *,
                 run_id: str, actor: str) -> None:  # fmt: skip
    from cairnward.audit.log import digest

    audit.append(
        "research.ask",
        actor=actor,
        target=prep.library,
        environment=prep.environment,
        outcome="ok",
        run_id=run_id,
        details={
            "engine": "paperqa",
            "question_digest": digest(question),
            "sources": len(answer.sources),
            "passages": answer.passages,
            "uncited": answer.uncited,
            "cannot_answer": answer.cannot_answer,
        },
    )


def ask(cfg: Config, library: str, question: str, *, effort: str | None = None,
        evidence_k: int = 10, skip_summary: bool = False, log: Any = print) -> Any:  # fmt: skip
    """From the CLI: a run key from the running core, the worker in this process."""
    from cairnward.audit import AuditLog
    from cairnward.run.runner import RunError, management

    prep = prepare(cfg, library, effort)
    srt = sandbox_command(cfg, prep.environment)
    try:
        mgmt = management(cfg)
    except RunError as exc:
        raise EngineError(str(exc)) from None
    with mgmt:
        resp = mgmt.post("/v1/runs", json={
            "environment": prep.environment, "client": "paperqa", "profile": "generic",
            "workspace": str(prep.work), "program": "paperqa", "arg_count": 0,
            "sandbox": "srt" if srt else "none"})  # fmt: skip
        if resp.status_code != 200:
            raise EngineError(f"Cairnward refused the run: {resp.text}")
        run = resp.json()
        started, code = time.monotonic(), 1
        try:
            result = execute(cfg, prep, question, gateway_port=run["gateway_port"],
                             key=run["gateway_key"], evidence_k=evidence_k,
                             skip_summary=skip_summary, log=log)  # fmt: skip
            code = 0
        finally:
            mgmt.post(
                f"/v1/runs/{run['run_id']}/end",
                json={"exit_code": code, "duration_s": time.monotonic() - started},
            )
    answer = finish(prep, question, result, time.monotonic() - started)
    with AuditLog.open(cfg.paths) as audit:
        audit_answer(audit, prep, question, answer, run_id=run["run_id"], actor="user")
    return answer


def _run(argv: list[str], spec: dict[str, Any], env: dict[str, str], work: Path) -> tuple[int, str]:
    with (work / "worker.log").open("w", encoding="utf-8") as errlog:
        res = subprocess.run(argv, input=json.dumps(spec), capture_output=False,
                             stdout=subprocess.PIPE, stderr=errlog, text=True, env=env,
                             cwd=work, timeout=6 * 3600, check=False)  # fmt: skip
    return res.returncode, res.stdout
