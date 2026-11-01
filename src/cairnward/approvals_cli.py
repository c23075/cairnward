"""`cairnward approvals`: answer what Cairnward is waiting to ask you (spec §5.3, M2).

Talks to the running core's inbox over the management socket. In watch mode (the
default) it waits for requests, shows each one and reads the answer:
  y  yes (a connection from a run: for the rest of that run; otherwise this once)
  c  yes, for this conversation        e  yes, for this environment
  n  no (also what happens when nobody answers in time)
High-risk and user-only requests can't be remembered: only y or n.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, TextIO

import httpx

ANSWERS = {"y": (True, "once"), "c": (True, "conversation"), "e": (True, "environment"),
           "n": (False, "once")}  # fmt: skip


def describe(item: dict[str, Any]) -> str:
    where = [f"environment {item['environment']}" if item.get("environment") else "",
             f"client {item['client']}" if item.get("client") else "",
             f"run {str(item['run_id'])[:8]}" if item.get("run_id") else "",
             f"started by {item['initiated_by']}"]  # fmt: skip
    lines = [
        f"[{item['id']}] {item['risk']} risk, waiting {item['age_s']:.0f}s",
        f"  action : {item['kind']}",
        f"  target : {item['target']}",
        f"  why    : {item['reason']}",
        f"  where  : {', '.join(w for w in where if w)}",
    ]
    if item.get("provenance"):
        lines.append(
            "  NOTE   : untrusted content entered the conversation since your last message:"
        )
        lines += [f"           - {p}" for p in item["provenance"]]
    return "\n".join(lines)


def options(item: dict[str, Any]) -> str:
    yes = "this run" if item.get("run_id") and item["kind"] == "net.connect" else "once"
    if item.get("rememberable"):
        return f"[y] yes, {yes}  [c] this conversation  [e] this environment  [n] no"
    return f"[y] yes, {yes}  [n] no"


def parse_answer(reply: str, item: dict[str, Any]) -> tuple[bool, str] | None:
    reply = reply.strip().lower()
    reply = {"yes": "y", "no": "n"}.get(reply, reply)
    if reply in ("c", "e") and not item.get("rememberable"):
        return None
    if reply == "c" and not item.get("conversation_id"):
        return None
    return ANSWERS.get(reply)


def send(mgmt: httpx.Client, request_id: str, granted: bool, scope: str) -> bool:
    resp = mgmt.post(f"/v1/approvals/{request_id}", json={"granted": granted, "scope": scope})
    return resp.status_code == 200


def _notify(item: dict[str, Any]) -> None:
    exe = shutil.which("notify-send")
    if exe:
        subprocess.run(  # noqa: S603 - fixed program, our own text
            [exe, "--app-name=Cairnward", "Cairnward: approval needed",
             f"{item['kind']} {item['target']}"],
            check=False, capture_output=True, timeout=5,
        )  # fmt: skip


def list_pending(mgmt: httpx.Client, out: TextIO, *, as_json: bool) -> int:
    pending = mgmt.get("/v1/approvals").json()["pending"]
    if as_json:
        out.write(json.dumps(pending, indent=2) + "\n")
    elif not pending:
        out.write("nothing waiting for an answer\n")
    else:
        for item in pending:
            out.write(describe(item) + "\n")
    return 0


def watch(mgmt: httpx.Client, stdin: TextIO, out: TextIO, *, notify: bool = True,
          once: bool = False) -> int:  # fmt: skip
    """Answer requests as they arrive. `once`: stop when nothing is pending."""
    version, seen = 0, set[str]()
    out.write("Waiting for approval requests (Ctrl+C to stop)...\n")
    out.flush()
    while True:
        data = mgmt.get("/v1/approvals/wait", params={"version": version, "timeout": 25},
                        timeout=40).json()  # fmt: skip
        version = data["version"]
        pending = data["pending"]
        if once and not pending:
            return 0
        for item in pending:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            if notify:
                _notify(item)
            out.write("\n" + describe(item) + "\n")
            while True:
                out.write(f"  {options(item)}: ")
                out.flush()
                line = stdin.readline()
                if not line:  # end of input
                    return 0
                answer = parse_answer(line, item)
                if answer is not None:
                    break
                out.write("  (not one of the options)\n")
            ok = send(mgmt, item["id"], *answer)
            out.write(("  -> " + ("allowed" if answer[0] else "denied")) if ok
                      else "  -> too late: it was answered elsewhere or timed out")  # fmt: skip
            out.write("\n")
            out.flush()
