"""Finding the sandbox, and refusing to go on without one (fail closed, FR-ISO-5)."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cairnward.config.paths import Paths
from cairnward.sandbox import srt


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    monkeypatch.delenv("CAIRNWARD_SRT", raising=False)
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def no_tools(monkeypatch: pytest.MonkeyPatch, **found: str) -> None:
    monkeypatch.setattr(shutil, "which", found.get)


def installed(paths: Paths) -> Path:
    cli = srt.install_dir(paths) / srt.CLI_REL
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    return cli


def test_lookup_order(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    no_tools(monkeypatch)
    assert srt.locate(paths) is None
    no_tools(monkeypatch, srt="/usr/bin/srt")
    assert srt.locate(paths) == ["/usr/bin/srt"]
    cli = installed(paths)
    assert srt.locate(paths) == ["/usr/bin/srt"]  # the pinned copy is useless without node
    no_tools(monkeypatch, srt="/usr/bin/srt", node="/usr/bin/node")
    assert srt.locate(paths) == ["/usr/bin/node", str(cli)]
    monkeypatch.setenv("CAIRNWARD_SRT", "node 'a b/cli.js'")
    assert srt.locate(paths) == ["node", "a b/cli.js"]


def test_require_refuses_rather_than_running_unsandboxed(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_tools(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(srt.SandboxUnavailable, match="cairnward sandbox install"):
        srt.require(paths)
    monkeypatch.setattr(sys, "platform", "win32")
    no_tools(monkeypatch, srt="/usr/bin/srt")
    with pytest.raises(srt.SandboxUnavailable, match="Linux only"):
        srt.require(paths)


@pytest.mark.parametrize(("output", "expected"), [("v22.12.0\n", (22, 12)), ("nonsense", None)])
def test_node_version(monkeypatch: pytest.MonkeyPatch, output: str,
                      expected: tuple[int, int] | None) -> None:  # fmt: skip
    no_tools(monkeypatch, node="/usr/bin/node")

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    assert srt.node_version() == expected


def test_node_version_when_node_is_missing_or_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    no_tools(monkeypatch)
    assert srt.node_version() is None
    no_tools(monkeypatch, node="/usr/bin/node")

    def broken(cmd: list[str], **kw: object) -> None:
        raise OSError("exec failed")

    monkeypatch.setattr(subprocess, "run", broken)
    assert srt.node_version() is None


@pytest.mark.parametrize("version", [None, (20, 0), (22, 11)])
def test_install_refuses_an_old_node(paths: Paths, monkeypatch: pytest.MonkeyPatch,
                                     version: tuple[int, int] | None) -> None:  # fmt: skip
    monkeypatch.setattr(srt, "node_version", lambda: version)
    with pytest.raises(srt.SandboxUnavailable, match=r"Node\.js >= 22\.12"):
        srt.install(paths)
    assert not srt.install_dir(paths).exists()


def test_install_needs_npm_and_uses_the_lockfile(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srt, "node_version", lambda: (22, 12))
    no_tools(monkeypatch)
    with pytest.raises(srt.SandboxUnavailable, match="npm not found"):
        srt.install(paths)
    no_tools(monkeypatch, npm="/usr/bin/npm")
    calls: list[list[str]] = []

    def run(cmd: list[str], **kw: object) -> None:
        calls.append(cmd)

    monkeypatch.setattr(subprocess, "run", run)
    assert srt.install(paths) == srt.install_dir(paths) / srt.CLI_REL
    assert calls == [["/usr/bin/npm", "ci", "--no-audit", "--no-fund", "--omit=dev"]]  # hash-pinned
    assert (srt.install_dir(paths) / "package-lock.json").exists()
