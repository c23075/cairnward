"""`cairnward mcp`: a stdio MCP server for clients that only start local commands.

Reads newline-delimited JSON-RPC from stdin, sends each message to the gateway's MCP
endpoint with your API key, and writes the replies to stdout.
"""

from __future__ import annotations

import json
from typing import Any, TextIO

import httpx


def _error_line(line: str, message: str) -> str | None:
    try:
        rid = json.loads(line).get("id")
    except (ValueError, AttributeError):
        rid = None
    if rid is None:
        return None
    return json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": message}})


def run_shim(url: str, key: str, stdin: TextIO, stdout: TextIO,
             client: httpx.Client | None = None) -> int:  # fmt: skip
    own = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(None, connect=10))
    session: str | None = None
    try:
        for line in stdin:
            if not line.strip():
                continue
            headers: dict[str, Any] = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            if session:
                headers["Mcp-Session-Id"] = session
            try:
                resp = http.post(url, content=line.encode(), headers=headers)
            except httpx.HTTPError as exc:
                out = _error_line(line, f"Cairnward isn't reachable at {url}: {exc}")
            else:
                if sid := resp.headers.get("mcp-session-id"):
                    session = sid
                if resp.status_code == 202 or not resp.content:
                    continue
                try:
                    out = json.dumps(resp.json(), ensure_ascii=False)
                except ValueError:
                    out = _error_line(line, f"HTTP {resp.status_code} from Cairnward")
            if out:
                stdout.write(out + "\n")
                stdout.flush()
    finally:
        if own:
            http.close()
    return 0
