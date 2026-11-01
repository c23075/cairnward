"""A small stdio MCP server for tests (no dependencies; run with `python -I`).

Tools: echo, write_note (writes into a folder given as an argument), fetch (HTTP GET
through the proxy settings). FAKE_MCP_DESC replaces echo's description (a "rug pull").
It also prints a log line to stdout and asks the client for roots, as real servers do.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any


def send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


TOOLS = [
    {
        "name": "echo",
        "description": os.environ.get("FAKE_MCP_DESC", "Repeat the text back."),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_note",
        "description": "Write a note file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
        },
    },
    {
        "name": "fetch",
        "description": "GET a URL.",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}},
    },
]


def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        return {"content": [{"type": "text", "text": args["text"]}]}
    if name == "write_note":
        try:
            with Path(args["path"]).open("w") as fh:
                fh.write(args["text"])
            return {"content": [{"type": "text", "text": "written"}]}
        except OSError as exc:
            return {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
    if name == "fetch":
        try:
            body = urllib.request.urlopen(args["url"], timeout=20).read().decode()
            return {"content": [{"type": "text", "text": body}]}
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
    raise KeyError(name)


print("fake MCP server starting (a log line on stdout)", flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        if method == "notifications/initialized":
            send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
        continue
    if method is None:  # a response to our roots/list
        continue
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": msg["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "fake",
                        "version": os.environ.get("FAKE_MCP_VERSION", "1.0"),
                    },
                },
            }
        )
    elif method == "tools/list":
        cursor = (msg.get("params") or {}).get("cursor")
        page = TOOLS[:2] if cursor is None else TOOLS[2:]
        result: dict[str, Any] = {"tools": page}
        if cursor is None:
            result["nextCursor"] = "p2"
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    elif method == "tools/call":
        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": call(msg["params"]["name"], msg["params"].get("arguments", {})),
                }
            )
        except KeyError:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "no tool"}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": method}})
