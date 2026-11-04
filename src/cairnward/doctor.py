"""`cairnward doctor`: check prerequisites and that protections actually work (FR-ISO-5).

Each check reports ok / warn / fail with a fix. The isolation self-test runs a probe in
the real sandbox and expects direct network, DNS and reading a hidden folder to fail.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from cairnward.config.loader import Config, ConfigError, load_config
from cairnward.config.paths import Paths
from cairnward.sandbox import MIN_NODE_TEXT, SandboxSpec, locate, node_version, wrap
from cairnward.sandbox.srt import windows_paths
from cairnward.sysinfo import meminfo, on_windows_drive, wsl_interop_on, wsl_version

Status = Literal["ok", "warn", "fail", "skip"]
GB = 1024**3
# Rough KV-cache cost per context token for the default main model (Qwen3-30B-A3B, fp16),
# from the reference-machine benchmark (docs/bench/). Used only for the memory estimate.
KV_BYTES_PER_TOKEN = 100 * 1024
WSL_BACKEND_HINT = (
    "; under WSL2, run it inside WSL too: 127.0.0.1 here isn't Windows' own unless "
    "networkingMode=mirrored is set"
)
WSL_MEMORY_HINT = "; WSL2 gets half of Windows' memory by default: raise memory= in .wslconfig"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


def _meminfo() -> dict[str, int]:
    return meminfo()


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _running(cfg: Config) -> bool:
    sock = cfg.paths.state_dir / "cairnward.sock"
    if not sock.exists():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), timeout=2) as c:
            return c.get("http://cairnward/v1/status").status_code == 200
    except httpx.HTTPError:
        return False


# ------------------------------------------------------------------ checks


def check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 12)
    return Check(
        "python",
        "ok" if ok else "fail",
        f"{v.major}.{v.minor}.{v.micro}",
        "" if ok else "Python 3.12+ is required (uv python install 3.12)",
    )


def check_ports(cfg: Config) -> list[Check]:
    if _running(cfg):
        return [Check("ports", "ok", "Cairnward is running and holds its ports")]
    s = cfg.main.server
    busy = [
        f"{name} {p}"
        for name, p in (
            ("gateway", s.gateway_port),
            ("dashboard", s.dashboard_port),
            ("egress", s.egress_port),
        )
        if not _port_free(p)
    ]
    if busy:
        return [
            Check(
                "ports",
                "fail",
                f"in use by another program: {', '.join(busy)}",
                "stop that program or change the ports in cairnward.toml [server]",
            )
        ]
    return [
        Check(
            "ports",
            "ok",
            f"gateway {s.gateway_port}, dashboard {s.dashboard_port} and egress "
            f"{s.egress_port} are free",
        )
    ]


def check_backends(cfg: Config) -> list[Check]:
    out: list[Check] = []
    sizes: dict[str, int] = {}
    for name, b in cfg.backends.backends.items():
        if warning := b.unencrypted_warning(name):
            out.append(Check(f"backend {name}", "warn", "not encrypted, not contacted", warning))
            continue
        if b.location != "local":
            out.append(Check(f"backend {name}", "skip", f"{b.location} backend: not contacted"))
            continue
        try:
            if b.driver == "ollama":
                resp = httpx.get(f"{b.url.rstrip('/')}/api/tags", timeout=5, trust_env=False)
                models = {m["name"]: int(m.get("size", 0)) for m in resp.json().get("models", [])}
            else:
                resp = httpx.get(f"{b.url.rstrip('/')}/models", timeout=5, trust_env=False)
                models = {m["id"]: 0 for m in resp.json().get("data", [])}
        except (httpx.HTTPError, ValueError) as exc:
            out.append(
                Check(
                    f"backend {name}",
                    "fail",
                    f"not reachable at {b.url} ({type(exc).__name__})",
                    "start the model server (e.g. `systemctl start ollama`)"
                    + (WSL_BACKEND_HINT if wsl_version() else ""),
                )
            )
            continue
        sizes.update(models)
        out.append(Check(f"backend {name}", "ok", f"{len(models)} model(s) at {b.url}"))
    for role, rm in cfg.backends.roles.items():
        b = cfg.backends.backends[rm.backend]
        if b.location != "local" or not any(
            c.name == f"backend {rm.backend}" and c.status == "ok" for c in out
        ):
            continue
        present = rm.model in sizes or (b.driver == "ollama" and f"{rm.model}:latest" in sizes)
        out.append(
            Check(
                f"model {role}",
                "ok" if present else "fail",
                f"{rm.model} on {rm.backend}",
                "" if present else f"ollama pull {rm.model}",
            )
        )
    out.append(_memory_check(cfg, sizes))
    return out


def _memory_check(cfg: Config, sizes: dict[str, int]) -> Check:
    mem = _meminfo()
    if not mem:
        return Check("memory", "skip", "can't read memory information on this system")
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    main = cfg.backends.roles.get("main")
    fast = cfg.backends.roles.get("fast")
    if main is None or main.model not in sizes:
        return Check("memory", "skip", f"{total / GB:.1f} GB RAM; model sizes unknown")
    ctx = max((e.context_tokens for e in cfg.environments.values()), default=8192)
    need_main = sizes[main.model] + ctx * KV_BYTES_PER_TOKEN
    need_both = need_main + (sizes.get(fast.model, 0) if fast else 0)
    detail = (
        f"main ~{need_main / GB:.1f} GB at {ctx} tokens, with fast model "
        f"~{need_both / GB:.1f} GB; {total / GB:.1f} GB total, {avail / GB:.1f} GB available"
    )
    if need_main + 3 * GB > total:
        hint = WSL_MEMORY_HINT if wsl_version() else ""
        return Check("memory", "fail", detail, f"use a smaller model or context; or add RAM{hint}")
    if need_both > avail:
        return Check(
            "memory",
            "warn",
            detail,
            "close heavy apps before long runs, or set OLLAMA_MAX_LOADED_MODELS=1 "
            "(the fast model is then loaded on demand)",
        )
    return Check("memory", "ok", detail)


def _isolation_requested(cfg: Config) -> bool:
    return any(cfg.privacy(e).value("isolation") == "on" for e in [None, *cfg.environments])


def check_isolation(cfg: Config) -> list[Check]:
    if not _isolation_requested(cfg):
        return [Check("isolation", "skip", "isolation is off everywhere")]
    if not sys.platform.startswith("linux"):
        return [
            Check(
                "isolation",
                "fail",
                f"not supported on {sys.platform} yet",
                'use WSL2 on Windows (later milestone), or set isolation = "off"',
            )
        ]
    out: list[Check] = []
    for tool, pkg in (("bwrap", "bubblewrap"), ("socat", "socat"), ("rg", "ripgrep")):
        found = shutil.which(tool)
        out.append(
            Check(
                tool,
                "ok" if found else "fail",
                found or "not found",
                "" if found else f"sudo apt install {pkg}",
            )
        )
    node = node_version()
    ok = node is not None and node >= (22, 12)
    out.append(
        Check(
            "node",
            "ok" if ok else "fail",
            ".".join(map(str, node)) if node else "not found",
            "" if ok else f"install Node.js {MIN_NODE_TEXT}+ (e.g. with fnm)",
        )
    )
    srt = locate(cfg.paths)
    out.append(
        Check(
            "sandbox-runtime",
            "ok" if srt else "fail",
            " ".join(srt) if srt else "not installed",
            "" if srt else "cairnward sandbox install",
        )
    )
    restrict = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    if restrict.exists() and restrict.read_text().strip() == "1":
        out.append(
            Check(
                "user namespaces",
                "fail",
                "restricted by AppArmor (Ubuntu 24.04+)",
                "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0",
            )
        )
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        out.append(
            Check(
                "user",
                "warn",
                "running as root",
                "run Cairnward as your normal user; sandboxes are weaker under root",
            )
        )
    if srt and all(c.status != "fail" for c in out):
        out.append(self_test(srt))
    return out


PROBE = r"""
import json, os, socket, subprocess, sys
def blocked(fn):
    try:
        fn(); return False
    except Exception:
        return True
def empty(path):  # a hidden folder is an empty one, a hidden file an empty one
    try:
        return not (os.listdir(path) if os.path.isdir(path) else os.path.getsize(path))
    except OSError:
        return True
def windows_program_refused():  # "MZ" is what the kernel hands to /init as a Windows program
    with open("probe.exe", "wb") as f:
        f.write(b"MZ" + bytes(62))
    os.chmod("probe.exe", 0o755)
    try:
        subprocess.run(["./probe.exe"], capture_output=True, timeout=10)
    except OSError:
        return True
    except subprocess.SubprocessError:
        pass
    return False
got = {
    "direct_tcp": blocked(lambda: socket.create_connection(("1.1.1.1", 443), timeout=3)),
    "dns": blocked(lambda: socket.getaddrinfo("example.com", 443)),
    "hidden_folder": blocked(lambda: open(sys.argv[1]).read()),
    "unix_socket": blocked(lambda: socket.socket(socket.AF_UNIX)),
}
if sys.argv[2:]:  # under WSL
    got["windows_paths"] = all(empty(p) for p in sys.argv[2:])
    got["windows_programs"] = windows_program_refused()
    got["vm_socket"] = blocked(lambda: socket.socket(socket.AF_VSOCK))
print(json.dumps(got))
"""


def self_test(srt: list[str]) -> Check:
    with tempfile.TemporaryDirectory(prefix="cairnward-doctor-") as tmp:
        base = Path(tmp)
        hidden = base / "hidden"
        hidden.mkdir()
        (hidden / "secret").write_text("x")
        (base / "probe.py").write_text(PROBE)
        spec = SandboxSpec(allowed_hosts=[], allow_write=[base / "ws"], deny_read=[hidden])
        (base / "ws").mkdir()
        windows = [str(p) for p in windows_paths() if p.exists()]
        cmd = wrap(
            srt,
            spec.write(base / "srt.json"),
            [sys.executable, "-I", str(base / "probe.py"), str(hidden / "secret"), *windows],
        )
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
                cwd=base / "ws",
                check=False,
            )
            got = json.loads(res.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return Check(
                "isolation self-test",
                "fail",
                "the probe didn't run in the sandbox",
                "run `cairnward doctor --json` and check the sandbox prerequisites",
            )
    leaks = [k for k, v in got.items() if not v]
    if leaks:
        return Check(
            "isolation self-test",
            "fail",
            f"NOT blocked inside the sandbox: {', '.join(leaks)}",
            "don't rely on isolation until this passes; report it",
        )
    blocked_here = "direct network, DNS, hidden folder and Unix sockets"
    if "windows_paths" in got:
        blocked_here += ", Windows drives, Windows programs and VM sockets"
    return Check("isolation self-test", "ok", f"{blocked_here} blocked")


def check_wsl(cfg: Config) -> list[Check]:
    """What's different under WSL. Nothing to report elsewhere."""
    version = wsl_version()
    if version is None:
        return []
    if version == 1:
        return [
            Check(
                "WSL",
                "fail",
                "this is WSL1, which can't run the sandbox",
                "in Windows: wsl --set-version <distro> 2",
            )
        ]
    out = [Check("WSL", "ok", "WSL2; Windows drives and programs are hidden from sandboxes")]
    p = cfg.paths
    on_drive = [str(d) for d in (p.config_dir, p.data_dir, p.state_dir) if on_windows_drive(d)]
    if on_drive:
        out.append(
            Check(
                "WSL folders",
                "fail",
                f"on a Windows drive: {', '.join(on_drive)}",
                "keep them in the Linux file system: SQLite's locks, Unix sockets and file "
                "permissions don't work reliably on a Windows drive",
            )
        )
    if wsl_interop_on():
        out.append(
            Check(
                "WSL interop",
                "warn",
                "Linux programs here can start Windows programs",
                "sandboxes block this, but unsandboxed programs can: set [interop] "
                "enabled=false in /etc/wsl.conf if you don't need it",
            )
        )
    return out


def check_data(cfg: Config) -> list[Check]:
    out: list[Check] = []
    usage = shutil.disk_usage(cfg.paths.data_dir if cfg.paths.data_dir.exists() else Path.home())
    free = usage.free / GB
    out.append(
        Check(
            "disk",
            "ok" if free > 2 else "warn",
            f"{free:.1f} GB free",
            "" if free > 2 else "free some space; the vault and models need room",
        )
    )
    if (cfg.paths.data_dir / "audit.db").exists():
        from cairnward.audit import AuditLog

        with AuditLog.open(cfg.paths) as log:
            res = log.verify()
        out.append(
            Check(
                "audit log",
                "ok" if res.ok else "fail",
                f"{res.records} records, chain {'intact' if res.ok else 'BROKEN'}",
                "" if res.ok else "cairnward audit verify  (shows what changed)",
            )
        )
    out.append(check_anchoring(cfg))
    return out


def check_anchoring(cfg: Config) -> Check:
    if cfg.privacy().value("audit_anchor") != "git":
        return Check("audit anchoring", "skip", 'audit_anchor is "off"')
    from cairnward.audit.anchor import Anchorer

    anchorer = Anchorer(cfg.paths, cfg.main.anchor)
    if not cfg.main.anchor.repo or not anchorer.install_file.exists():
        return Check("audit anchoring", "warn", 'audit_anchor = "git" but it isn\'t set up',
                     "cairnward audit anchor --setup")  # fmt: skip
    if not (cfg.paths.data_dir / "audit.db").exists():
        return Check("audit anchoring", "ok", f"to {cfg.main.anchor.repo}")
    from cairnward.audit import AuditLog

    with AuditLog.open(cfg.paths) as log:
        last = log.latest("audit.anchor")
    if last is None or last.outcome != "ok":
        detail = "no checkpoint pushed yet" if last is None else "the last attempt failed"
        return Check("audit anchoring", "warn", detail, "cairnward audit anchor")
    return Check(
        "audit anchoring", "ok", f"last checkpoint {last.ts[:19]} to {cfg.main.anchor.repo}"
    )


def check_disk_encryption() -> Check:
    if not sys.platform.startswith("linux") or not shutil.which("lsblk"):
        return Check("disk encryption", "skip", "not checked on this system")
    try:
        out = subprocess.run(
            ["lsblk", "-rno", "TYPE"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return Check("disk encryption", "skip", "lsblk failed")
    if "crypt" in out.split():
        return Check("disk encryption", "ok", "an encrypted volume (LUKS) is in use")
    if wsl_version():
        return Check(
            "disk encryption",
            "warn",
            "can't be seen from inside WSL",
            "WSL's disk is a file on the Windows drive: turn on BitLocker or Device Encryption "
            "in Windows; recommended, not required",
        )
    return Check(
        "disk encryption",
        "warn",
        "no encrypted volume found",
        "full-disk encryption (LUKS) protects unlocked conversations if the disk is "
        "stolen; recommended, not required",
    )


def run_checks(paths: Paths) -> list[Check]:
    checks = [check_python()]
    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        return [
            *checks,
            Check(
                "configuration", "fail", f"{len(exc.problems)} problem(s)", "cairnward config check"
            ),
        ]
    checks.append(Check("configuration", "ok", f"privacy {cfg.privacy().label} (global)"))
    checks += check_ports(cfg)
    checks += check_backends(cfg)
    checks += check_wsl(cfg)
    checks += check_isolation(cfg)
    checks += check_data(cfg)
    checks.append(check_file_modes(cfg))
    checks.append(check_disk_encryption())
    checks.append(check_citation_engine(cfg))
    checks.append(check_backup(cfg))
    if cfg.main.replication.mode != "none":
        checks.append(check_replication(cfg))
    checks += check_parsers(cfg)
    return checks


def check_parsers(cfg: Any) -> list[Check]:
    """Docling and GROBID, when a library uses them (D68)."""
    import json

    from cairnward.research.parsing import options_file

    try:
        opts = json.loads(options_file(cfg.paths.data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    uses = {o.get(k) for o in opts.values() if isinstance(o, dict) for k in ("parser", "metadata")}
    out = []
    if "docling" in uses:
        from cairnward.research.engines import docling

        if docling.python(cfg.paths) is None:
            out.append(Check("docling", "fail", "a library uses Docling but it isn't installed",
                             "cairnward research engine install docling"))  # fmt: skip
        else:
            problems = docling.verify(cfg.paths)
            out.append(
                Check(
                    "docling",
                    "warn" if problems else "ok",
                    "; ".join(problems) or "installed, unchanged since install",
                )
            )
    if "grobid" in uses:
        from cairnward.research.grobid import Grobid, GrobidError

        url = cfg.main.research.grobid_url
        if not url:
            out.append(Check("grobid", "fail", "a library uses GROBID but grobid_url isn't set",
                             '[research] grobid_url = "http://127.0.0.1:8070"'))  # fmt: skip
        else:
            try:
                g = Grobid(url, timeout=5)
                alive = g.alive()
                g.close()
            except GrobidError as exc:
                out.append(Check("grobid", "fail", str(exc)))
            else:
                out.append(Check("grobid", "ok" if alive else "warn",
                                 f"{url} {'answers' if alive else 'is not answering'}",
                                 "" if alive else "start it: docker run --rm --network none "
                                 "-p 127.0.0.1:8070:8070 grobid/grobid:<version>"))  # fmt: skip
    return out


def check_file_modes(cfg: Any) -> Check:
    from cairnward.config.paths import wider_than_owner

    wide = wider_than_owner(cfg.paths)
    if wide:
        return Check("file modes", "warn", f"{len(wide)} readable by others, e.g. {wide[0]}",
                     "starting cairnward serve fixes this")  # fmt: skip
    return Check("file modes", "ok", "owner-only")


def check_replication(cfg: Any) -> Check:
    from cairnward import replication

    st = replication.status(cfg)
    if not st["configured"]:
        return Check("replication", "fail", "turned on, but this device hasn't joined a folder",
                     "cairnward replicate init <folder>, or join")  # fmt: skip
    if st["problem"]:
        return Check("replication", "fail", st["problem"])
    waiting = [d["id"][:10] for d in st["devices"] if d["status"] == "pending"]
    if waiting:
        return Check("replication", "warn", f"device(s) waiting for approval: {', '.join(waiting)}",
                     f"cairnward replicate approve {waiting[0]}")  # fmt: skip
    last = st["last"]
    if last is None:
        return Check("replication", "warn", "no sync yet", "cairnward replicate sync")
    if last["problems"]:
        return Check("replication", "warn", f"last sync: {last['problems'][0]}")
    detail = f"{len(st['devices'])} device(s), last sync {last['time']}"
    if st["cloud_warning"]:
        detail += f"; {st['cloud_warning']}"
    return Check("replication", "ok", detail)


def check_backup(cfg: Any) -> Check:
    from datetime import UTC, datetime

    from cairnward import backup

    b = cfg.main.backup
    if b.mode == "none":
        return Check("backup", "warn", "off: conversations, library and artifacts aren't backed up",
                     "cairnward backup use /a/folder/on/another/disk")  # fmt: skip
    try:
        st = backup.status(cfg)
    except backup.BackupError as exc:
        return Check("backup", "fail", str(exc))
    if not st["restic"]:
        return Check("backup", "fail", "restic isn't installed", "e.g. sudo apt install restic")
    if st.get("problem") or not st.get("initialised"):
        return Check("backup", "fail", st.get("problem") or "repository not set up",
                     "cairnward backup init")  # fmt: skip
    notes = []
    if st.get("same_disk_warning"):
        notes.append(st["same_disk_warning"])
    if not b.password_command:
        notes.append("the password is only on this disk unless you keep a copy elsewhere "
                     f"({backup.password_file(cfg)})")  # fmt: skip
    if st["pending_purges"]:
        notes.append(f"{st['pending_purges']} purged conversation(s) still to remove from backups")
    last = st["last"]
    if last is None:
        return Check("backup", "warn", "no backup yet", "cairnward backup run")
    age_h = (datetime.now(UTC) - datetime.fromisoformat(last["time"])).total_seconds() / 3600
    if b.mode == "auto" and age_h > 2 * b.interval_hours + 1:
        notes.append(f"last backup {age_h:.0f} h ago")
    if st.get("cloud_warning"):
        notes.append(st["cloud_warning"])
    detail = f"{b.mode}, {st.get('snapshots', '?')} snapshots, last {last['time'][:16]}"
    return Check("backup", "warn" if notes else "ok",
                 detail + ("; " + "; ".join(notes) if notes else ""))  # fmt: skip


def check_citation_engine(cfg: Any) -> Check:
    from cairnward.research import csl

    setting = cfg.main.research.citation_engine
    found = csl.available(cfg.paths)
    try:
        used = csl.choose(setting, "apa", cfg.paths)
    except csl.CslError as exc:
        return Check("citation engine", "fail", f"{setting}: {exc}")
    if used == "builtin":
        return Check("citation engine", "warn" if setting == "auto" else "ok",
                     f"{setting}: built-in APA/IEEE only",
                     "for Zotero-identical output and other styles: cairnward research engine "
                     "install citeproc-js (or install pandoc)")  # fmt: skip
    return Check("citation engine", "ok",
                 f"{setting}: {used} (installed: {', '.join(found)})")  # fmt: skip
