"""Follow-ups from the OpenShell review: the program a run starts is pinned; a run is
stopped when Cairnward goes away mid-run; risky allow-list entries are flagged."""

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.config.privacy import allow_host_risk
from cairnward.run.runner import RunError, _watch_server, check_program


def paths_with(tmp_path: Path, pinning: str) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    p.config_dir.mkdir(parents=True, exist_ok=True)
    (p.config_dir / "cairnward.toml").write_text(f'[privacy]\nmcp_pinning = "{pinning}"\n')
    return p


def test_program_is_pinned(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("print('v1')\n")
    command = [sys.executable, str(script)]
    said: list[str] = []
    cfg = load_config(paths_with(tmp_path, "on"))
    first = check_program(cfg, "coding", command, trust_change=False, log=said.append)
    assert check_program(cfg, "coding", command, trust_change=False, log=said.append) == first
    script.write_text("print('v2: something else')\n")
    with pytest.raises(RunError, match="changed since it last ran"):
        check_program(cfg, "coding", command, trust_change=False, log=said.append)
    accepted = check_program(cfg, "coding", command, trust_change=True, log=said.append)
    assert accepted != first and not said
    # pinning off: a warning, not a refusal; other environments are pinned separately
    cfg = load_config(paths_with(tmp_path, "off"))
    script.write_text("print('v3')\n")
    check_program(cfg, "coding", command, trust_change=False, log=said.append)
    assert said and "changed since it last ran in 'coding'" in said[0]
    check_program(cfg, "scratch", command, trust_change=False, log=said.append)
    assert len(said) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_run_stops_when_cairnward_goes_away() -> None:
    class Gone:
        def get(self, *a: Any, **k: Any) -> Any:
            raise OSError("no socket")

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)  # fmt: skip
    said: list[str] = []
    stop = threading.Event()
    started = time.monotonic()
    _watch_server(Gone(), proc, stop, said.append, every_s=0.05)
    assert proc.wait(10) != 0 and time.monotonic() - started < 5
    assert "stopping the agent" in said[0]


def test_watcher_leaves_a_healthy_run_alone() -> None:
    class Healthy:
        def get(self, *a: Any, **k: Any) -> Any:
            return type("R", (), {"status_code": 200})()

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    stop = threading.Event()
    t = threading.Thread(target=_watch_server, args=(Healthy(), proc, stop, print, 0.05))
    t.start()
    assert proc.wait(10) == 0
    stop.set()
    t.join(5)


@pytest.mark.parametrize(("entry", "risky"), [
    ("169.254.169.254", "metadata"), ("metadata.google.internal:80", "metadata"),
    ("[fd00:ec2::254]:80", "metadata"), ("*", "everything"), ("*.com", "everything"),
    ("127.0.0.1:8080", "this machine"), ("192.168.1.10", "local network"),
    ("10.0.0.0/8", "local network"), ("printer.local", "local services"),
    ("api.openalex.org", None), ("*.arxiv.org", None), ("1.1.1.1:443", None),
])  # fmt: skip
def test_allow_list_risks(entry: str, risky: str | None) -> None:
    found = allow_host_risk(entry)
    assert (found is None) if risky is None else (found is not None and risky in found)


def test_risks_show_in_privacy_warnings(tmp_path: Path) -> None:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    p.config_dir.mkdir(parents=True)
    (p.config_dir / "cairnward.toml").write_text(
        '[privacy]\nnetwork = "allowlist"\nnetwork_allow_hosts = ["169.254.169.254"]\n'
    )
    warnings = load_config(p).privacy().warnings
    assert any("cloud metadata service" in w for w in warnings)
