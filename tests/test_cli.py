from pathlib import Path

import pytest

from cairnward import __version__
from cairnward.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: cairnward" in capsys.readouterr().out


def test_serve_says_which_port_is_busy(tmp_path: Path) -> None:
    import socket

    from cairnward.config import load_config
    from cairnward.config.paths import Paths
    from cairnward.core.server import serve

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "st")
    p.config_dir.mkdir(parents=True)
    (p.config_dir / "cairnward.toml").write_text(f"[server]\negress_port = {port}\n")
    said: list[str] = []
    try:
        assert serve(load_config(p), log=said.append) == 2
    finally:
        holder.close()
    assert f"egress_port {port}" in said[0] and "[server]" in said[0]
