"""A real `cairnward serve` in each server mode, for end-to-end tests.

Modes (the ways `serve` can actually run):
- `isolated`: isolation on; the core runs inside the sandbox and the outer process runs
  egress, forwarders, sandboxed agents, MCP servers.
- `hybrid`: isolation off globally but on for the `coding` environment; a single process,
  with agents and MCP servers of that environment still sandboxed.
- `off`: isolation off everywhere; a single process, nothing sandboxed.

Every serve flow should run in all modes where it makes sense, so a mode can't silently
lose a feature (the job runner once didn't start in single-process mode).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cairnward.config.paths import Paths
from cairnward.sandbox import locate

MODES = ["isolated", "hybrid", "off"]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


class Cairnward:
    """Config, environment and a running `serve` for one mode."""

    def __init__(self, tmp_path: Path, mode: str, *, privacy: str = 'preset = "strict"\n',
                 main: str = "", backend_url: str = "http://127.0.0.1:9",
                 models: tuple[str, str, str | None] = ("m", "m", None),
                 coding_env: str = "") -> None:  # fmt: skip
        assert mode in MODES
        self.mode, self.tmp = mode, tmp_path
        self.paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
        self.env = {**os.environ, "CAIRNWARD_CONFIG_DIR": str(self.paths.config_dir),
                    "CAIRNWARD_DATA_DIR": str(self.paths.data_dir),
                    "CAIRNWARD_STATE_DIR": str(self.paths.state_dir),
                    "CAIRNWARD_CACHE_DIR": str(tmp_path / "cache")}  # fmt: skip
        if mode != "off" and locate(self.paths) is None:
            pytest.skip("sandbox-runtime not installed (cairnward sandbox install / CAIRNWARD_SRT)")
        self.gw, self.dash, self.eg = free_port(), free_port(), free_port()
        envs = self.paths.config_dir / "environments"
        envs.mkdir(parents=True, exist_ok=True)
        off = "" if mode == "isolated" else 'isolation = "off"\n'
        (self.paths.config_dir / "cairnward.toml").write_text(
            f"[server]\ngateway_port = {self.gw}\ndashboard_port = {self.dash}\n"
            f"egress_port = {self.eg}\n{main}[privacy]\n{privacy}{off}"
        )
        if coding_env or mode == "hybrid":
            # a file replaces the built-in environment, so start from its settings
            from cairnward.config.models import builtin_environments

            base = builtin_environments()["coding"].model_dump(exclude_defaults=True)
            top = "".join(f"{k} = {json.dumps(v)}\n" for k, v in base.items()
                          if f"{k} =" not in coding_env)  # fmt: skip
            hybrid = '[privacy]\nisolation = "on"\n' if mode == "hybrid" else ""
            (envs / "coding.toml").write_text(top + coding_env + hybrid)
        main_m, fast_m, embed_m = models
        backends = (f'[backends.local-ollama]\ndriver = "ollama"\nurl = "{backend_url}"\n'
                    f'[roles.main]\nbackend = "local-ollama"\nmodel = "{main_m}"\n'
                    f'[roles.fast]\nbackend = "local-ollama"\nmodel = "{fast_m}"\n')  # fmt: skip
        if embed_m:
            backends += f'[roles.embed]\nbackend = "local-ollama"\nmodel = "{embed_m}"\n'
        (self.paths.config_dir / "backends.toml").write_text(backends)
        self.serve: subprocess.Popen[str] | None = None

    # ---------------------------------------------------------------- facts per mode

    @property
    def core_isolated(self) -> bool:
        return self.mode == "isolated"

    @property
    def coding_sandboxed(self) -> bool:
        """Are `cairnward run --env coding` and its MCP servers sandboxed?"""
        return self.mode in ("isolated", "hybrid")

    # ---------------------------------------------------------------- running things

    def cw(self, *args: str, check: bool = False, timeout: float | None = 120,
           **kw: Any) -> subprocess.CompletedProcess[str]:  # fmt: skip
        return subprocess.run([sys.executable, "-m", "cairnward", *args], env=self.env,
                              capture_output=True, text=True, check=check, timeout=timeout,
                              **kw)  # fmt: skip

    def popen(self, *args: str, **kw: Any) -> subprocess.Popen[str]:
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE)
        return subprocess.Popen([sys.executable, "-m", "cairnward", *args], env=self.env,
                                text=True, **kw)  # fmt: skip

    def __enter__(self) -> Cairnward:
        self.serve = self.popen("serve", stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        st = self.cw("status", "--wait", "60")
        expected = "core isolated" if self.core_isolated else "single process"
        if expected not in st.stdout:
            self.__exit__()
            out = self.serve.stdout.read() if self.serve.stdout else ""
            raise AssertionError(f"{self.mode}: {st.stdout}{st.stderr}\nserve: {out[-3000:]}")
        return self

    def __exit__(self, *exc: object) -> None:
        if self.serve is None:
            return
        self.serve.terminate()
        try:
            self.serve.wait(20)
        except subprocess.TimeoutExpired:
            self.serve.kill()
            self.serve.wait(5)

    def audit(self, last: int = 300) -> list[dict[str, Any]]:
        out = self.cw("audit", "log", "--json", "--last", str(last), check=True).stdout
        return [json.loads(x) for x in out.splitlines()]
