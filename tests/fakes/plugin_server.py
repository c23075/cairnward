"""A small plugin for tests (stdio MCP, no dependencies; run with `python -I`). Its tools use the
core's services, or try to go around them, and report what happened."""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GATEWAY = os.environ.get("CAIRNWARD_GATEWAY_URL", "")
KEY = os.environ.get("CAIRNWARD_PLUGIN_KEY", "")
TOOLS = [
    {"name": name, "description": text,
     "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}}}
    for name, text in (("through_fetch", "Fetch a URL through the core."),
                       ("direct", "Fetch a URL without the core."),
                       ("save", "Save an artifact."), ("env", "Report the environment."),
                       ("write", "Write a file."))
]  # fmt: skip


def call(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(GATEWAY + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out: dict[str, Any] = json.load(resp)
            return resp.status, out
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode(errors="replace")[:300]}


def run(tool: str, value: str) -> str:
    if tool == "through_fetch":
        status, out = call("/cairnward/fetch", {"url": value})
        if status != 200:
            return f"refused {status} {out['detail']}"
        return "got " + base64.b64decode(out["body_b64"]).decode()
    if tool == "direct":
        try:
            with urllib.request.urlopen(value, timeout=5) as resp:
                return "reached " + str(resp.read().decode())
        except Exception as exc:
            return f"blocked {type(exc).__name__}"
    if tool == "save":
        status, out = call("/cairnward/artifacts", {
            "path": value, "content_b64": base64.b64encode(b"saved").decode()})  # fmt: skip
        return f"{status} {out}"
    if tool == "write":
        try:
            Path(value).write_text("x")
            return "written"
        except OSError as exc:
            return f"blocked {type(exc).__name__}"
    return json.dumps({"environment": os.environ.get("CAIRNWARD_ENVIRONMENT"),
                       "contract": os.environ.get("CAIRNWARD_CONTRACT"),
                       "data": os.environ.get("CAIRNWARD_PLUGIN_DATA")})  # fmt: skip


def main() -> None:
    for line in sys.stdin:
        msg = json.loads(line)
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:
            continue
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-plugin", "version": "1"},
            }  # fmt: skip
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            text = run(msg["params"]["name"], msg["params"]["arguments"].get("value", ""))
            result = {"content": [{"type": "text", "text": text}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
