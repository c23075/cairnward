"""What a run can read in the real sandbox, with the home folder hidden, and that real agents
still start. Skipped unless srt is installed; agents are skipped unless installed."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.run import PROFILES, plan_run
from cairnward.sandbox import locate

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

RUN = {"run_id": "r1", "gateway_key": "cw-run-k", "egress_token": "tok", "gateway_port": 8484,
       "egress_port": 8486}  # fmt: skip

PROBE = r"""
import json, os, sys
def can_read(p):
    try:
        if os.path.isdir(p):
            os.listdir(p)
        else:
            with open(p, "rb") as f:
                return bool(f.read(1))
        return True
    except OSError:
        return False
def can_write(p):
    try:
        with open(p, "w") as f:
            f.write("x")
        return True
    except OSError:
        return False
print("RESULT " + json.dumps({"read": {p: can_read(p) for p in sys.argv[1].split(":")},
                              "write": {p: can_write(p) for p in sys.argv[2].split(":")},
                              "home": sorted(os.listdir(os.path.expanduser("~")))}))
"""


def start(tmp_path: Path, workspace: Path, command: list[str], *, privacy: str = "",
          profile: str = "generic",
          path: str | None = None) -> subprocess.CompletedProcess[str]:  # fmt: skip
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    srt = locate(paths)
    if srt is None:
        pytest.skip("sandbox-runtime not installed (cairnward sandbox install / CAIRNWARD_SRT)")
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "environments" / "e.toml").write_text(
        f'[privacy]\npreset = "strict"\n{privacy}\n'
    )
    run_tmp = tmp_path / "run"
    run_tmp.mkdir(exist_ok=True)
    plan = plan_run(load_config(paths), environment="e", profile=PROFILES[profile],
                    model="cw/e:medium", workspace=workspace, command=command, run=RUN,
                    srt=srt, settings_file=run_tmp / "srt.json", tmp_dir=run_tmp)  # fmt: skip
    env = {**plan.env, "PATH": path} if path else plan.env
    return subprocess.run(plan.argv, env=env, cwd=workspace, capture_output=True, text=True,
                          timeout=180, check=False)  # fmt: skip


def probe(tmp_path: Path, workspace: Path, reads: list[Path], writes: list[Path],
          **kw: Any) -> dict[str, Any]:  # fmt: skip
    script = workspace / "probe.py"
    script.write_text(PROBE)
    res = start(tmp_path, workspace, [sys.executable, str(script), ":".join(map(str, reads)),
                                      ":".join(map(str, writes))], **kw)  # fmt: skip
    line = next((x for x in res.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, res.stdout + res.stderr
    out: dict[str, Any] = json.loads(line[7:])
    return out


def plant(path: Path, text: str = "secret") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CAIRNWARD_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    return home


def test_home_folder_hidden_except_what_the_run_needs(tmp_path: Path, home: Path) -> None:
    ws = home / "projects" / "ws"
    ws.mkdir(parents=True)
    hidden = [plant(home / n) for n in (
        ".config/rclone/rclone.conf", ".bash_history", ".pypirc", ".npmrc",
        ".cargo/credentials.toml", ".thunderbird/profile/key4.db",
        ".config/BraveSoftware/Local State", ".var/app/org.signal.Signal/config.json",
        ".ssh/id_ed25519", ".env",
        # nothing lists these: only hiding the home folder covers them
        "projects/other/unpublished.tex", "Documents/tax.pdf", ".someapp/token",
    )]  # fmt: skip
    # inside what the run may read and write: the deeper deny still wins
    hidden += [plant(home / ".cache/cairnward/paperqa/lib/index.pkl"),
               plant(home / ".config/git/credentials"),
               plant(home / ".cache/mozilla/firefox/cache2")]  # fmt: skip
    # a link in the workspace is no way round
    link = ws / "docs"
    link.symlink_to(home / "projects" / "other")
    file_link = ws / "key"
    file_link.symlink_to(home / ".ssh" / "id_ed25519")
    visible = [plant(home / ".gitconfig", "[user]\n"), plant(home / ".config/git/ignore"),
               plant(ws / "main.py"), plant(home / ".cache/pip/x")]  # fmt: skip
    out = probe(tmp_path, ws, [*hidden, link / "unpublished.tex", file_link, *visible],
                [ws / "w.txt", home / ".cache" / "w.txt", home / "w.txt",
                 home / ".cache/cairnward/paperqa/lib/index.pkl", home / ".gitconfig"],
                )  # fmt: skip
    read = out["read"]
    assert [p for p in map(str, hidden) if read[p]] == []
    assert read[str(link / "unpublished.tex")] is False and read[str(file_link)] is False
    assert all(read[str(p)] for p in visible), read
    write = out["write"]
    assert write[str(ws / "w.txt")] and write[str(home / ".cache" / "w.txt")]
    # the hidden home is a throwaway tmpfs: a write there succeeds and never reaches the disk
    assert not (home / "w.txt").exists()
    assert not write[str(home / ".gitconfig")]
    assert (home / ".gitconfig").read_text() == "[user]\n"
    assert (home / ".cache/cairnward/paperqa/lib/index.pkl").read_text() == "secret"
    # what the agent sees of the home folder is only what was re-allowed
    assert "Documents" not in out["home"] and ".someapp" not in out["home"]


def test_opt_out_reads_the_home_folder_but_not_listed_secrets(tmp_path: Path, home: Path) -> None:
    ws = home / "ws"
    ws.mkdir()
    doc, key = plant(home / "Documents/tax.pdf"), plant(home / ".ssh/id_ed25519")
    conf = plant(home / ".config/rclone/rclone.conf")
    out = probe(tmp_path, ws, [doc, key, conf], [ws / "w"], privacy='home_access = "readable"')
    assert out["read"] == {str(doc): True, str(key): False, str(conf): False}


def test_workspace_is_the_home_folder(tmp_path: Path, home: Path) -> None:
    doc, key = plant(home / "Documents/tax.pdf"), plant(home / ".ssh/id_ed25519")
    conf, hist = plant(home / ".config/rclone/rclone.conf"), plant(home / ".bash_history")
    out = probe(tmp_path, home, [doc, key, conf, hist], [home / "w"])
    assert out["read"] == {str(doc): True, str(key): False, str(conf): False, str(hist): False}


def test_program_through_linked_folders_starts(tmp_path: Path, home: Path) -> None:
    """As `uv tool install` lays it out, with what made bwrap refuse to start: the script's
    interpreter is reached through a symlinked folder inside a re-allowed toolchain folder."""
    python = home / ".local/share/uv/python"
    real = python / "cpython-3.12.16" / "bin"
    real.mkdir(parents=True)
    shutil.copy("/bin/sh", real / "sh")
    (python / "cpython-3.12").symlink_to("cpython-3.12.16")
    tool = home / ".local/share/uv/tools/agent/bin"
    tool.mkdir(parents=True)
    (tool.parent / "pyvenv.cfg").write_text("")
    (tool / "sh").symlink_to(python / "cpython-3.12" / "bin" / "sh")
    (tool / "agent").write_text(f"#!{tool}/sh\necho started; cat ~/.gitconfig; cat ~/.env\n")
    (tool / "agent").chmod(0o755)
    (home / ".local/bin").mkdir(parents=True)
    (home / ".local/bin/agent").symlink_to(tool / "agent")
    # dotfile managers link settings into the home folder
    plant(home / "dotfiles/gitconfig", "from-dotfiles\n")
    (home / ".gitconfig").symlink_to(home / "dotfiles/gitconfig")
    plant(home / ".env", "KEY=leaked\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    res = start(tmp_path, ws, [str(home / ".local/bin/agent")])
    assert "started" in res.stdout and "from-dotfiles" in res.stdout, res.stdout + res.stderr
    assert "leaked" not in res.stdout


def test_real_home_folder(tmp_path: Path) -> None:
    """The real home: the Python, srt and node that start the run live there on many machines.
    Only names are looked at, and nothing is printed from any file."""
    home = Path.home()
    ws = tmp_path / "ws"
    ws.mkdir()
    names = (".bash_history", ".zsh_history", ".config/rclone", ".ssh", ".mozilla", ".var/app",
             ".pypirc", ".npmrc", "Documents", "Downloads")  # fmt: skip
    reads = [home / n for n in names if (home / n).exists()]
    # this checkout: its .venv is re-allowed to start Python, the files beside it aren't
    repo = Path(__file__).resolve().parents[2]
    if repo.is_relative_to(home):
        reads.append(repo / "pyproject.toml")
    out = probe(tmp_path, ws, reads, [ws / "w"])
    assert not any(out["read"].values()), out["read"]


def test_npm_global_layout_starts(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in for `npm install -g --prefix` under the home folder: a link in bin/ to a
    script in lib/node_modules that names node through env and loads a sibling. The prefix is
    on no list: only following the link re-allows it."""
    if shutil.which("node") is None:
        pytest.skip("node isn't on PATH")
    prefix = home / ".npm-global"
    (prefix / "bin").mkdir(parents=True)
    pkg = prefix / "lib/node_modules/@acme/agent"
    plant(pkg / "dep.js", "module.exports = 'loaded-dep';\n")
    plant(pkg / "dist/cli.js", "#!/usr/bin/env node\nconsole.log(require('../dep.js'));\n")
    (pkg / "dist/cli.js").chmod(0o755)
    (prefix / "bin/agent").symlink_to("../lib/node_modules/@acme/agent/dist/cli.js")
    plant(prefix / "etc/npmrc", "//registry/:_authToken=x\n")
    monkeypatch.setenv("PATH", f"{prefix / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    ws = tmp_path / "ws"
    ws.mkdir()
    res = start(tmp_path, ws, ["agent"], profile="qwen-code")
    assert "loaded-dep" in res.stdout, res.stdout + res.stderr


def agent_path() -> str:
    """PATH with node too: fnm's isn't on it in every shell."""
    fnm = sorted(Path.home().glob(".local/share/fnm/node-versions/*/installation/bin"))
    return os.pathsep.join([*map(str, fnm[-1:]), os.environ.get("PATH", "")])


@pytest.mark.parametrize(("profile", "program"), [("aider", "aider"), ("qwen-code", "qwen")])
def test_real_agent_starts(tmp_path: Path, profile: str, program: str) -> None:
    path = agent_path()
    exe = shutil.which(program, path=path)
    if exe is None:
        pytest.skip(f"{program} isn't installed")
    ws = tmp_path / "ws"
    ws.mkdir()
    res = start(tmp_path, ws, [exe, "--version"], profile=profile, path=path)
    assert res.returncode == 0, res.stdout + res.stderr
    assert any(c.isdigit() for c in res.stdout), res.stdout + res.stderr
