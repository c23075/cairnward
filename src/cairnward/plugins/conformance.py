"""The contract test suite (ADR-0017, rule 7): a plugin must pass before the core starts it.

`cairnward plugin verify <name>` starts a probe with the plugin's exact sandbox and key, then the
plugin itself, against a running Cairnward, and records the pass for this code's digest and this
contract version. A plugin's own CI runs the same command against the core version it declares.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from cairnward.config.loader import Config
from cairnward.plugins.contract import CONTRACT_VERSION, ENV_KEY


@dataclass(frozen=True)
class Result:
    name: str
    passed: bool
    detail: str = ""


def record_file(cfg: Config, name: str) -> Path:
    return cfg.paths.state_dir / "plugins" / f"{name}.json"


def not_verified(cfg: Config, name: str) -> str | None:
    """Why this plugin hasn't passed the suite for its current code, or None."""
    server = cfg.mcp.servers[name]
    try:
        record = json.loads(record_file(cfg, name).read_text())
    except (OSError, ValueError):
        record = {}
    if record.get("sha256") == server.sha256 and record.get("contract") == CONTRACT_VERSION:
        return None
    return (f"{name} hasn't passed the plugin contract's tests for this code: run "
            f"`cairnward plugin verify {name}`")  # fmt: skip


def record_pass(cfg: Config, name: str) -> None:
    path = record_file(cfg, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sha256": cfg.mcp.servers[name].sha256,
                                "contract": CONTRACT_VERSION, "at": int(time.time())}))  # fmt: skip
    path.chmod(0o600)


# Runs inside the plugin's sandbox with the system's Python, standard library only. Each check
# prints one JSON line: what it tried, and whether that was possible.
PROBE = r"""
import json, os, socket, sys, urllib.request
def report(name, value): print(json.dumps({"name": name, "possible": value}), flush=True)
def connect(host, port):
    try:
        socket.create_connection((host, port), timeout=3).close()
        return True
    except OSError:
        return False
def through_proxy(url, *reached):
    try:
        urllib.request.urlopen(url, timeout=5).read(1)
        return True
    except urllib.error.HTTPError as exc:
        return exc.code in reached  # otherwise the sandbox's proxy refused it
    except Exception:
        return False
def write(path, keep=False):
    try:
        with open(path, "w") as f: f.write("x")
        if not keep: os.unlink(path)
        return True
    except OSError:
        return False
def read(path):
    try:
        with open(path) as f: f.read(1)
        return True
    except OSError:
        return False
home, data, outside, secret, gateway = sys.argv[1:6]
report("direct_network", connect("1.1.1.1", 443))
report("proxy_network", through_proxy("http://example.com/"))
write(os.path.join(home, ".cairnward-probe"), keep=True)  # looked for from outside the sandbox
write(os.path.join(outside, "cairnward-probe"), keep=True)
report("read_home", read(secret))
report("write_data", write(os.path.join(data, "cairnward-probe")))
report("gateway", through_proxy(gateway + "/models", 401))
"""


def _probe(cfg: Config, name: str, environment: str, key: str, gateway: str,
           srt: list[str] | None, outside: Path, secret: Path) -> list[Result]:  # fmt: skip
    from cairnward.mcp.launch import plan_launch, server_dir

    data = server_dir(cfg, name)
    with tempfile.TemporaryDirectory(prefix="cw-plugin-verify-") as tmp:
        script = Path(tmp) / "probe.py"
        script.write_text(PROBE, encoding="utf-8")
        plan = plan_launch(
            cfg, name, environment, srt=srt, egress_url=None, tmp_dir=Path(tmp),
            extra_env={ENV_KEY: key}, verifying=True,
            command=[sys.executable, "-I", str(script), str(Path.home()), str(data), str(outside),
                     str(secret), gateway],
        )  # fmt: skip
        if not plan.sandboxed:
            return [Result("sandbox", False, "the plugin wouldn't run in the sandbox")]
        res = subprocess.run(plan.argv, env=plan.env, cwd=plan.cwd, capture_output=True,  # noqa: S603
                             text=True, timeout=120, check=False)  # fmt: skip
    seen = {}
    for line in res.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        seen[row["name"]] = row["possible"]
    # The sandbox hides the home folder behind a throwaway one, so a write there can succeed
    # and go nowhere: what counts is whether the file is really there afterwards.
    escaped = [p for p in (Path.home() / ".cairnward-probe", outside / "cairnward-probe")
               if p.exists()]  # fmt: skip
    for path in escaped:
        path.unlink()
    if len(seen) < 5:
        return [Result("probe ran", False, (res.stderr or res.stdout)[-300:])]
    return [
        Result("starts with no network", not seen["direct_network"] and not seen["proxy_network"]),
        Result("can't write outside its data folder", not escaped),
        Result("can't read the home folder", not seen["read_home"]),
        Result("can write its data folder", seen["write_data"]),
        Result("can reach the gateway", seen["gateway"]),
    ]  # fmt: skip


def _services(cfg: Config, name: str, environment: str, key: str, gateway: str) -> list[Result]:
    other = next((e for e in sorted(cfg.environments) if e != environment), None)
    server = cfg.mcp.servers[name]
    out = []
    with httpx.Client(base_url=gateway, headers={"Authorization": f"Bearer {key}"},
                      timeout=30, trust_env=False) as c:  # fmt: skip
        info = c.get("/cairnward/contract")
        out.append(Result("the key is accepted", info.status_code == 200, info.text[:200]))
        got = c.post("/cairnward/fetch", json={"url": "https://not-listed.invalid/"})
        out.append(Result("fetch refuses a host it doesn't list", got.status_code == 403,
                          got.text[:200]))  # fmt: skip
        if server.fetch_hosts and not server.fetch_hosts[0].startswith("*"):
            local = c.post("/cairnward/fetch", json={"url": "http://127.0.0.1:1/"})
            out.append(Result("fetch refuses this machine", local.status_code == 403))
        if other:
            model = c.post("/chat/completions", json={
                "model": f"cw/{other}",
                "messages": [{"role": "user", "content": "x"}]})  # fmt: skip
            out.append(Result("the key is refused in another environment",
                              model.status_code == 403, model.text[:200]))  # fmt: skip
    return out


async def _tools(cfg: Config, name: str, environment: str, extra_env: dict[str, str],
                 srt: list[str] | None) -> list[Result]:  # fmt: skip
    from cairnward.mcp.jsonrpc import MAX_MESSAGE
    from cairnward.mcp.launch import plan_launch
    from cairnward.mcp.pins import PinStore
    from cairnward.mcp.upstream import Connection

    pins = PinStore.at(cfg.paths.data_dir)
    with tempfile.TemporaryDirectory(prefix="cw-plugin-verify-") as tmp:
        plan = plan_launch(cfg, name, environment, srt=srt, egress_url=None, tmp_dir=Path(tmp),
                           extra_env=extra_env, verifying=True)  # fmt: skip
        with plan.log.open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                *plan.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=log, env=plan.env, cwd=plan.cwd, limit=MAX_MESSAGE,
            )  # fmt: skip
        assert proc.stdout is not None and proc.stdin is not None  # noqa: S101
        conn = Connection(proc.stdout, proc.stdin)
        try:
            await conn.initialize(timeout=120)
            tools = await conn.list_tools(timeout=60)
        except Exception as exc:
            return [Result("the plugin starts in its sandbox", False, f"{exc} (see {plan.log})")]
        finally:
            await conn.close()
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
    out = [Result("the plugin starts in its sandbox", True)]
    states = [pins.state(name, t, conn.raw_tools.get(t["name"])) for t in tools]
    # A new tool isn't a failure: the hub offers it to no one until `cairnward mcp approve`,
    # which itself needs the plugin to start.
    changed = [s.name for s in states if s.status == "changed"]
    flagged = [s.name for s in states if s.flags]
    hint = f"review with `cairnward mcp approve {name}`: {', '.join(changed)}"
    out.append(Result("its tool definitions match the pins", not changed, hint))
    out.append(Result("no suspicious text in its tools", not flagged, ", ".join(flagged)))
    return out


def verify(cfg: Config, name: str, environment: str, key: str, gateway: str,
           srt: list[str] | None) -> list[Result]:  # fmt: skip
    """Everything the contract asks of this plugin, on the real sandbox and a running core."""
    from cairnward.plugins import contract

    with tempfile.TemporaryDirectory(prefix="cw-plugin-outside-", dir=Path.home()) as outside:
        secret = Path(outside) / "probe-secret"
        secret.write_text("x")
        results = _probe(cfg, name, environment, key, gateway, srt, Path(outside), secret)
    results += _services(cfg, name, environment, key, gateway)
    extra = {contract.ENV_KEY: key, contract.ENV_GATEWAY: gateway,
             contract.ENV_ENVIRONMENT: environment,
             contract.ENV_CONTRACT: str(contract.CONTRACT_VERSION)}  # fmt: skip
    results += asyncio.run(_tools(cfg, name, environment, extra, srt))
    return results
