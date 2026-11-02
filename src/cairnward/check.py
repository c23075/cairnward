"""`cairnward check`: everything that needs your real machine and models, in one command
(T3.1), with one readable report.

Runs, against the running `cairnward serve`:
1. `doctor` (prerequisites, memory, isolation self-test in the real sandbox);
2. the lit.ask eval (`evals/pdf_qa`) with the lean engine, and PaperQA2 too when it's
   installed: the M1 exit criteria (page-cited answers, zero invented citations);
3. the coding eval (`evals/coding`) with each installed agent (Aider, Qwen Code, opencode,
   Goose);
4. what the audit log shows for each chat client (calls, errors, last use), so the
   manual client checklist can be filled in next to it.

Writes Markdown and JSON (default `docs/bench/check-<date>.md` in a repository checkout,
else `<data>/reports/`). The report holds no prompts, answers or host name: the machine
is described by CPU, memory and OS only. Evals need a repository checkout (they live in
`evals/`); without one they are reported as skipped.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from cairnward import __version__
from cairnward.config.paths import Paths

REPO = Path(__file__).resolve().parents[2]
AGENTS = {"aider": "aider", "qwen-code": "qwen", "opencode": "opencode", "goose": "goose"}


@dataclass
class Section:
    title: str
    status: str = "skip"  # pass | fail | skip
    lines: list[str] = field(default_factory=list)
    data: Any = None
    seconds: float = 0.0


def _cw(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "cairnward", *args], capture_output=True,  # noqa: S603
                          text=True, check=False, timeout=timeout)  # fmt: skip


def _module(path: Path, name: str) -> ModuleType | None:
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def machine() -> dict[str, Any]:
    cpu = platform.processor() or platform.machine()
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    ram = None
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                ram = round(int(line.split()[1]) / 1024 / 1024, 1)
    commit = None
    git = shutil.which("git")
    if (REPO / ".git").exists() and git:
        res = subprocess.run([git, "-C", str(REPO), "rev-parse", "--short", "HEAD"],  # noqa: S603
                             capture_output=True, text=True, check=False)  # fmt: skip
        commit = res.stdout.strip() or None
    return {"cpu": cpu, "cores": _cores(), "ram_gb": ram,
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(), "cairnward": __version__,
            "commit": commit}  # fmt: skip


def _cores() -> int | None:
    import os

    return os.cpu_count()


# ------------------------------------------------------------------ sections


def doctor_section(paths: Paths) -> Section:
    from cairnward.doctor import run_checks

    sec = Section("Doctor (prerequisites, memory, isolation self-test)")
    checks = run_checks(paths)
    sec.data = [asdict(c) for c in checks]
    for c in checks:
        mark = {"ok": "ok", "warn": "warn", "fail": "FAIL"}.get(c.status, c.status)
        sec.lines.append(f"| {c.name} | {mark} | {c.detail} |")
    sec.status = "fail" if any(c.status == "fail" for c in checks) else "pass"
    return sec


def server_section() -> tuple[Section, bool]:
    sec = Section("Server")
    st = _cw("status")
    up = st.returncode == 0
    sec.status = "pass" if up else "fail"
    first = (st.stdout or st.stderr).strip().splitlines()
    sec.lines.append(first[0] if first else "no answer from `cairnward status`")
    if not up:
        sec.lines.append("Start it with `uv run cairnward serve` in another terminal, then "
                         "run `cairnward check` again.")  # fmt: skip
    return sec, up


def models_section(paths: Paths) -> Section:
    from cairnward.config import load_config

    sec = Section("Models", status="pass")
    cfg = load_config(paths)
    roles = getattr(cfg.backends, "roles", {}) or {}
    for name, role in sorted(roles.items()):
        sec.lines.append(f"- {name}: `{role.model}` on `{role.backend}`")
    sec.data = {name: {"model": r.model, "backend": r.backend} for name, r in roles.items()}
    return sec


def pdf_qa_section(env: str, engines: list[str], log: Callable[[str], None]) -> Section:
    sec = Section(f"lit.ask eval ({', '.join(engines)}; environment {env})")
    mod = _module(REPO / "evals" / "pdf_qa" / "run_eval.py", "cw_eval_pdf_qa")
    if mod is None:
        sec.lines.append("skipped: needs a repository checkout (evals/pdf_qa)")
        return sec
    started = time.monotonic()
    try:
        out = mod.run(env, engines, emit=lambda line: log(f"  {line}"))
    except mod.EvalError as exc:
        sec.status = "fail"
        sec.lines.append(f"could not run: {exc}")
        return sec
    sec.seconds = round(time.monotonic() - started, 1)
    sec.data = out
    sec.lines += ["| Engine | Passed | Invented citations | Total seconds |", "|---|---|---|---|"]
    for engine, s in out["summaries"].items():
        sec.lines.append(f"| {engine} | {s['passed']}/{s['questions']} | "
                         f"{s['invented_citations']} | {s['seconds_total']} |")  # fmt: skip
    sec.lines += ["", "| Question | Engine | Pass | Cited page | Content | Honest | Seconds |",
                  "|---|---|---|---|---|---|---|"]  # fmt: skip
    for r in out["results"]:

        def mark(key: str, r: dict[str, Any] = r) -> str:
            return "" if key not in r else ("yes" if r[key] else "no")

        sec.lines.append(f"| {r['id']} | {r['engine']} | {mark('pass')} | {mark('cited')} | "
                         f"{mark('content')} | {mark('honest')} | {r['seconds']} |")  # fmt: skip
    lean = out["summaries"].get("lean")
    exit_ok = (
        bool(lean)
        and lean["passed"] == lean["questions"]
        and not any(s["invented_citations"] for s in out["summaries"].values())
    )
    sec.status = "pass" if exit_ok else "fail"
    sec.lines += ["", "M1 exit criteria (lean engine passes every question, zero invented "
                  f"citations): **{'met' if exit_ok else 'not met'}**"]  # fmt: skip
    return sec


def coding_section(agents: list[str], log: Callable[[str], None]) -> Section:
    sec = Section("Coding eval (fix a failing test through `cairnward run`)")
    mod = _module(REPO / "evals" / "coding" / "run_eval.py", "cw_eval_coding")
    if mod is None:
        sec.lines.append("skipped: needs a repository checkout (evals/coding)")
        return sec
    found = [p for p in agents if shutil.which(AGENTS[p])]
    missing = [p for p in agents if p not in found]
    if not found:
        sec.lines.append("skipped: no coding agent installed (" + ", ".join(
            f"`{AGENTS[p]}`" for p in agents) + ")")  # fmt: skip
        return sec
    task = json.loads((REPO / "evals" / "coding" / "fix_failing_test" / "task.json").read_text())
    sec.lines += ["| Agent | Pass | Seconds | Model calls | Prompt tokens | Completion tokens |",
                  "|---|---|---|---|---|---|"]  # fmt: skip
    results = []
    for profile in found:
        log(f"  coding eval: {profile} ...")
        r = mod.one(task, profile, mod.AGENT_COMMANDS[profile], None, "coding")
        results.append(r)
        sec.lines.append(f"| {profile} | {'yes' if r['passed'] else 'no'} | {r['seconds']} | "
                         f"{r['model_calls']} | {r['prompt_tokens']} | "
                         f"{r['completion_tokens']} |")  # fmt: skip
    if missing:
        sec.lines.append(f"\nNot installed, not run: {', '.join(missing)}")
    sec.data = results
    sec.status = "pass" if all(r["passed"] for r in results) else "fail"
    return sec


def review_section(env: str) -> Section:
    sec = Section(f"Review pipeline eval (environment {env})")
    script = REPO / "evals" / "litreview_table" / "run_eval.py"
    if not script.exists():
        sec.lines.append("skipped: needs a repository checkout (evals/litreview_table)")
        return sec
    started = time.monotonic()
    res = subprocess.run([sys.executable, str(script), "--env", env], capture_output=True,  # noqa: S603
                         text=True, check=False, cwd=REPO)  # fmt: skip
    sec.seconds = round(time.monotonic() - started, 1)
    rows = []
    for line in res.stdout.splitlines():
        with contextlib.suppress(ValueError):
            rows.append(json.loads(line))
    checks = [r for r in rows if "check" in r]
    if not checks:
        sec.status = "fail"
        sec.lines.append(f"could not run: {(res.stdout + res.stderr).strip()[-400:]}")
        return sec
    sec.data = rows
    sec.lines += ["| Check | Result |", "|---|---|"]
    sec.lines += [f"| {c['check']} | {'pass' if c['pass'] else 'FAIL'} |" for c in checks]
    sec.status = "pass" if all(c["pass"] for c in checks) else "fail"
    return sec


def clients_section() -> Section:
    sec = Section("Chat clients (from the audit log)")
    out = _cw("audit", "log", "--json", "--action", "model.call", "--last", "100000")
    if out.returncode != 0:
        sec.lines.append("could not read the audit log")
        return sec
    by: dict[str, dict[str, Any]] = {}
    for line in out.stdout.splitlines():
        rec = json.loads(line)
        if rec.get("phase") != "post" or rec.get("run_id") or not rec.get("client"):
            continue  # agent runs are in the coding section; Cairnward's own calls have no client
        c = by.setdefault(rec.get("client") or "?", {"calls": 0, "errors": 0, "last": ""})
        c["calls"] += 1
        c["errors"] += rec.get("outcome") != "ok"
        c["last"] = max(c["last"], rec.get("time") or rec.get("ts") or "")
    sec.data = by
    if not by:
        sec.lines.append("No chat client has made a model call yet. Connect one (docs/clients/) "
                         "and send a message, then run the check again.")  # fmt: skip
        return sec
    sec.status = "pass" if all(c["errors"] < c["calls"] for c in by.values()) else "fail"
    sec.lines += ["| Client | Model calls | Errors | Last call |", "|---|---|---|---|"]
    for name, c in sorted(by.items()):
        sec.lines.append(f"| {name} | {c['calls']} | {c['errors']} | {c['last'][:19]} |")
    checklist = REPO / "docs" / "clients" / "checklist.md"
    if checklist.exists():
        sec.lines += ["", "Manual checks (tick them here after trying each client):", ""]
        sec.lines += [x for x in checklist.read_text().splitlines() if x.startswith("|")]
    return sec


# ------------------------------------------------------------------ the report


def render(sections: list[Section], info: dict[str, Any], started: str) -> str:
    verdict = "fail" if any(s.status == "fail" for s in sections) else "pass"
    lines = [f"# Cairnward check, {started[:10]}", "",
             f"Overall: **{verdict}** · Cairnward {info['cairnward']}"
             + (f" ({info['commit']})" if info.get("commit") else "")
             + f" · {info['cpu']}, {info['cores']} threads, {info['ram_gb']} GB RAM, {info['os']}",
             "", "| Section | Result | Time |", "|---|---|---|"]  # fmt: skip
    for s in sections:
        lines.append(f"| {s.title} | {s.status} | {f'{s.seconds:.0f} s' if s.seconds else ''} |")
    for s in sections:
        lines += ["", f"## {s.title}", ""]
        if s.title.startswith("Doctor"):
            lines += ["| Check | Result | Detail |", "|---|---|---|"]
        lines += s.lines
    lines += ["", "Generated by `cairnward check`. Prompts and answers are not included."]
    return "\n".join(lines) + "\n"


def default_out(paths: Paths, day: str) -> Path:
    if (REPO / "docs" / "bench").is_dir():
        return REPO / "docs" / "bench" / f"check-{day}.md"
    return paths.data_dir / "reports" / f"check-{day}.md"


def _guarded(title: str, build: Callable[[], Section]) -> Section:
    """One section; an unexpected error is reported there and the check goes on."""
    try:
        return build()
    except Exception as exc:  # a bug in an eval or in Cairnward: keep the other sections
        import traceback

        sec = Section(title)
        sec.status = "fail"
        where = traceback.extract_tb(exc.__traceback__)[-1]
        at = f"{Path(where.filename).name}:{where.lineno}"
        sec.lines.append(f"crashed: {type(exc).__name__}: {str(exc)[:300]} ({at}); "
                         "please report this")  # fmt: skip
        return sec


def run(paths: Paths, *, env: str = "litreview-offline", paperqa: bool = True,
        coding: bool = True, review: bool = False, out: Path | None = None,
        log: Callable[[str], None] = print) -> int:  # fmt: skip
    started = datetime.now(UTC).isoformat(timespec="seconds")
    info = machine()
    sections: list[Section] = []
    log("doctor ...")
    sections.append(doctor_section(paths))
    server, up = server_section()
    sections.append(server)
    if up:
        sections.append(models_section(paths))
        from cairnward.research.engines import paperqa as pqa

        engines = ["lean"] + (["paperqa"] if paperqa and pqa.python(paths) else [])
        log(f"lit.ask eval ({', '.join(engines)}); on a CPU this takes a while ...")
        sections.append(
            _guarded(
                f"lit.ask eval ({', '.join(engines)}; environment {env})",
                lambda: pdf_qa_section(env, engines, log),
            )
        )
        if paperqa and "paperqa" not in engines:
            sections[-1].lines.append("\nPaperQA2 isn't installed, so it wasn't compared "
                                      "(`cairnward research engine install paperqa`).")  # fmt: skip
        if coding:
            sections.append(_guarded("Coding eval", lambda: coding_section(list(AGENTS), log)))
        if review:
            log("review pipeline eval (screening, notes, synthesis); on a CPU ~30 min ...")
            sections.append(_guarded("Review pipeline eval", lambda: review_section(env)))
        sections.append(_guarded("Chat clients", clients_section))
    text = render(sections, info, started)
    target = out or default_out(paths, started[:10])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    target.with_suffix(".json").write_text(json.dumps(
        {"started": started, "machine": info,
         "sections": [asdict(s) for s in sections]}, indent=1, default=str),
        encoding="utf-8")  # fmt: skip
    log(f"\nreport: {target}")
    for s in sections:
        log(f"  {s.status:4}  {s.title}")
    return 1 if any(s.status == "fail" for s in sections) else 0
