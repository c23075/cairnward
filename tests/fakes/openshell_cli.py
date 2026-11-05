"""A stand-in for the `openshell` CLI (OpenShell 0.1.x), for tests of sandbox = "openshell".

It implements the five commands Cairnward uses, with the behaviour OpenShell's documentation
describes: `sandbox create` needs a gateway and takes the policy file; `sandbox exec` runs in
the sandbox's working folder, sees none of the caller's environment, and returns the command's
exit code; `upload` keeps the source folder's name inside the working folder and keeps symlinks;
`download` only reads inside the working folder and places a folder cp-style; `delete` removes
the sandbox. A "sandbox" is a folder under $FAKE_OPENSHELL_STATE, so no isolation happens and
no network policy is enforced: it checks Cairnward's side of the contract, not OpenShell's.

$FAKE_OPENSHELL_FAIL names a command to fail (e.g. "download"). Every call is logged to
<state>/calls.jsonl.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ.get("FAKE_OPENSHELL_STATE", "/nonexistent"))


def fail(msg: str, code: int = 1) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def work(name: str) -> Path:
    return STATE / "sandboxes" / name / "sandbox"


def options(args: list[str], flags: set[str], valued: set[str]) -> tuple[dict[str, str], list[str]]:
    opts: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest += args[i:]
            break
        if a in valued:
            opts[a] = args[i + 1]
            i += 2
        elif a in flags:
            opts[a] = "1"
            i += 1
        elif a.startswith("-"):
            raise SystemExit(fail(f"unexpected argument '{a}'", 2))
        else:
            rest.append(a)
            i += 1
    return opts, rest


def create(args: list[str]) -> int:
    opts, rest = options(args, {"--detach"}, {"--name", "--policy", "--from"})
    if rest:
        return fail("this stand-in only creates detached sandboxes without a command", 2)
    policy = json.loads(Path(opts["--policy"]).read_text())  # JSON is YAML 1.2
    if policy.get("version") != 1 or "network_policies" not in policy:
        return fail("invalid policy")
    w = work(opts["--name"])
    if w.exists():
        return fail(f"sandbox {opts['--name']} already exists")
    w.mkdir(parents=True)
    (w.parent / "policy.yaml").write_text(json.dumps(policy))
    (w.parent / "image").write_text(opts.get("--from", ""))
    print(f"Created sandbox {opts['--name']}")
    return 0


def exec_(args: list[str]) -> int:
    opts, rest = options(args, {"--no-login-shell", "--tty", "--no-tty"}, {"-n", "--workdir"})
    w = work(opts["-n"])
    if not w.is_dir():
        return fail(f"sandbox {opts['-n']} not found")
    command = rest[1:] if rest and rest[0] == "--" else rest
    cwd = opts.get("--workdir", str(w))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(w)}  # not the caller's
    return subprocess.run(command, cwd=cwd, env=env, check=False).returncode


def upload(args: list[str]) -> int:
    _, rest = options(args, {"--no-git-ignore"}, set())
    name, src = rest[0], Path(rest[1])
    w = work(name)
    if not w.is_dir():
        return fail(f"sandbox {name} not found")
    shutil.copytree(src, w / src.name, symlinks=True, dirs_exist_ok=True)
    return 0


def download(args: list[str]) -> int:
    name, src, dest = args[0], args[1], Path(args[2])
    w = work(name)
    source = (w / src).resolve()
    if not source.is_relative_to(w.resolve()):
        return fail("source escapes the sandbox working directory")
    if not source.exists():
        return fail(f"{src}: not found")
    target = dest / source.name if dest.is_dir() else dest
    shutil.copytree(source, target, symlinks=True)
    return 0


def delete(args: list[str]) -> int:
    for name in args:
        shutil.rmtree(work(name).parent, ignore_errors=True)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "calls.jsonl").open("a") as f:
        f.write(json.dumps(argv) + "\n")
    if len(argv) < 2 or argv[0] != "sandbox":
        return fail("unsupported command", 2)
    cmd = argv[1]
    if os.environ.get("FAKE_OPENSHELL_FAIL") == cmd:
        return fail(f"{cmd} failed (FAKE_OPENSHELL_FAIL)")
    handlers = {"create": create, "exec": exec_, "upload": upload, "download": download,
                "delete": delete}  # fmt: skip
    if cmd not in handlers:
        return fail(f"unsupported command 'sandbox {cmd}'", 2)
    return handlers[cmd](argv[2:])


if __name__ == "__main__":
    sys.exit(main())
