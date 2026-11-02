"""Command-line entry point.

Subcommands so far: `config paths|check|schema` and `privacy show`. More (`serve`, `run`,
`doctor`, `audit`, `research`, ...) arrive with the Phase 0 tasks in docs/plan/phase0.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cairnward import __version__
from cairnward.config import SETTINGS, Config, ConfigError, default_paths, load_config
from cairnward.config.privacy import EffectivePrivacy
from cairnward.config.schema import render, schemas, write_schemas


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairnward",
        description="Local privacy gateway for AI clients and agents, with research workflows.",
    )
    parser.add_argument("--version", action="version", version=f"cairnward {__version__}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        help="configuration folder (default: $CAIRNWARD_CONFIG_DIR or ~/.config/cairnward)",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    cfg = sub.add_parser("config", help="inspect and validate configuration")
    cfg_sub = cfg.add_subparsers(dest="config_command", metavar="ACTION", required=True)
    cfg_sub.add_parser("paths", help="show where Cairnward keeps its files")
    cfg_sub.add_parser("check", help="validate all configuration files")
    schema = cfg_sub.add_parser("schema", help="write JSON Schemas for the config files")
    schema.add_argument("--out", type=Path, default=Path("schemas"), help="output folder")
    schema.add_argument(
        "--check", action="store_true", help="only check that the folder is up to date"
    )

    audit = sub.add_parser("audit", help="the hash-chained audit log")
    audit_sub = audit.add_subparsers(dest="audit_command", metavar="ACTION", required=True)
    verify = audit_sub.add_parser("verify", help="check the chain and the mirror file")
    verify.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="SEQ:HASH",
        help="a head you noted earlier (from `audit head`); repeatable",
    )
    audit_sub.add_parser(
        "head", help="print the latest record as SEQ:HASH (note it somewhere safe)"
    )
    verify.add_argument(
        "--anchors", action="store_true", help="also check against the anchors repo ([anchor])"
    )
    anch = audit_sub.add_parser("anchor", help="sign and push a checkpoint to your anchors repo")
    anch.add_argument("--setup", action="store_true", help="create the keys and show what to do")
    log_cmd = audit_sub.add_parser("log", help="show records")
    log_cmd.add_argument("--last", type=int, default=20, help="number of records (default 20)")
    log_cmd.add_argument("--action", help="only this action, e.g. settings.changed")
    log_cmd.add_argument("--conversation", help="only records for this conversation")
    log_cmd.add_argument("--json", action="store_true", help="one JSON record per line")
    eg_rep = audit_sub.add_parser("egress", help="what left this machine, per destination")
    for cmd in (log_cmd, eg_rep):
        cmd.add_argument("--run", help="only this run (id or its first characters)")
        cmd.add_argument("--client", help="only this client")
        cmd.add_argument("--env", help="only this environment")
        cmd.add_argument("--since", help="e.g. 24h, 7d, or an ISO date")
    eg_rep.add_argument("--conversation", help="only model calls of this conversation")
    fmt = eg_rep.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--csv", action="store_true")

    pol = sub.add_parser("policy", help="what the policy engine would decide")
    pol_sub = pol.add_subparsers(dest="policy_command", metavar="ACTION", required=True)
    explain = pol_sub.add_parser("explain", help="decision for an action, without running it")
    explain.add_argument(
        "kind", help="model.call, net.connect, fs.read, fs.write, exec, encrypt, sync.change, ..."
    )
    explain.add_argument("target", help="backend name, host[:port], path or command")
    explain.add_argument("--env", help="environment (default: the default environment)")
    explain.add_argument(
        "--by", choices=["user", "model"], default="model", help="who starts it (default: model)"
    )

    serve = sub.add_parser("serve", help="run the gateway and cairnward-egress (foreground)")
    serve.add_argument("--no-egress", action="store_true", help="don't start cairnward-egress")
    core = sub.add_parser("_core")  # internal: the isolated core, started by `serve`
    core.add_argument("--egress-control", type=Path, required=True)

    run = sub.add_parser("run", help="run a coding agent in the sandbox, pointed at the gateway")
    run.add_argument("--env", help="environment (default: the default environment)")
    run.add_argument("--profile", default="generic", help="aider, qwen-code or generic")
    run.add_argument("--model", help="virtual model (default: cw/<env>:<env's default effort>)")
    run.add_argument("--workspace", type=Path, help="writable folder (default: current folder)")
    run.add_argument("--client", help="client name in the audit log (default: the profile)")
    run.add_argument("--max-requests", type=int, help="stop the agent's model calls after N")
    run.add_argument("--max-tokens", type=int, help="... after N tokens (prompt + reply)")
    run.add_argument("--max-minutes", type=float, help="... after N minutes")
    run.add_argument(
        "agent_command",
        nargs=argparse.REMAINDER,
        metavar="-- COMMAND",
        help="the agent and its arguments, after --",
    )

    doc = sub.add_parser("doctor", help="check prerequisites and that protections work")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    bk = sub.add_parser("backup", help="encrypted backups with restic (vault, library, artifacts)")
    bk_sub = bk.add_subparsers(dest="backup_command", metavar="ACTION", required=True)
    bk_sub.add_parser("init", help="create the repository set in [backup] repository")
    bk_sub.add_parser("run", help="back up now (and remove purged conversations from old backups)")
    bk_sub.add_parser("list", help="the snapshots")
    bk_sub.add_parser("status", help="mode, repository, last backup, pending removals")
    bk_res = bk_sub.add_parser("restore", help="bring back conversations from a backup")
    bk_res.add_argument("--snapshot", help="snapshot ID (default: the latest)")
    bk_res.add_argument("--at", help="the latest backup from this date or earlier (YYYY-MM-DD)")
    bk_res.add_argument("--to", type=Path, help="only extract the backup into this folder")
    chk = sub.add_parser("check", help="doctor + evals + client checks on this machine, one report")
    chk.add_argument("--env", default="litreview-offline", help="environment for the lit.ask eval")
    chk.add_argument("--no-paperqa", action="store_true", help="don't compare with PaperQA2")
    chk.add_argument("--no-coding", action="store_true", help="skip the coding-agent eval")
    chk.add_argument("--review", action="store_true",
                     help="also run the review-pipeline eval (long on a CPU)")  # fmt: skip
    chk.add_argument("--out", type=Path, help="report path (default docs/bench/check-<date>.md)")
    stp = sub.add_parser("setup", help="choose a privacy preset and check prerequisites")
    stp.add_argument("--preset", choices=["open", "balanced", "strict"])
    stp.add_argument(
        "--install-sandbox",
        action="store_true",
        help="install the sandbox if the preset needs it and it's missing",
    )

    mcp = sub.add_parser(
        "mcp", help="MCP tools: stdio bridge for clients (default), servers, review and approve"
    )
    mcp.add_argument("--env", help="environment (default: the client's or the default one)")
    mcp.add_argument("--url", help="MCP endpoint (default: $CAIRNWARD_MCP_URL or the gateway)")
    mcp.add_argument("--key", help="API key (default: $CAIRNWARD_API_KEY)")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", metavar="ACTION")
    mcp_sub.add_parser("list", help="configured MCP servers and their permissions")
    mcp_tools = mcp_sub.add_parser("tools", help="a server's tools, pin state and warnings")
    mcp_tools.add_argument("server")
    mcp_appr = mcp_sub.add_parser("approve", help="review and pin a server's tool definitions")
    mcp_appr.add_argument("server")
    mcp_appr.add_argument("--yes", action="store_true", help="approve without asking")
    mcp_sub.add_parser("connect", help="stdio bridge to the MCP endpoint (same as no ACTION)")

    vlt = sub.add_parser("vault", help="Cairnward's copy of conversations: list, search, export")
    vlt_sub = vlt.add_subparsers(dest="vault_command", metavar="ACTION")
    v_list = vlt_sub.add_parser("list", help="conversations, newest first (default)")
    v_list.add_argument("--env")
    v_list.add_argument("--trash", action="store_true", help="what's in the trash")
    v_list.add_argument("--limit", type=int, default=50)
    v_search = vlt_sub.add_parser("search", help="find text in titles and messages")
    v_search.add_argument("text")
    v_search.add_argument("--env")
    v_show = vlt_sub.add_parser("show", help="print a conversation")
    v_show.add_argument("id", help="id or its first characters")
    v_show.add_argument("--json", action="store_true")
    v_exp = vlt_sub.add_parser("export", help="export a conversation (plain text)")
    v_exp.add_argument("id")
    v_exp.add_argument("--format", choices=["md", "json"], default="md")
    v_exp.add_argument("-o", "--output", help="file to write (default: print)")
    v_actions = (
        ("delete", "move to the trash (30 days)"),
        ("restore", "take out of the trash"),
        ("purge", "destroy now (key first; can't be undone)"),
    )
    for name, text in v_actions:
        cmd = vlt_sub.add_parser(name, help=text)
        cmd.add_argument("id")
        if name == "purge":
            cmd.add_argument("--force", action="store_true", help="even if not in the trash")
    v_pin = vlt_sub.add_parser("pin", help="pin (kept by retention); --off to unpin")
    v_pin.add_argument("id")
    v_pin.add_argument("--off", action="store_true")
    v_title = vlt_sub.add_parser("title", help="rename a conversation")
    v_title.add_argument("id")
    v_title.add_argument("title")
    vlt_sub.add_parser("prune", help="apply retention now and empty old trash")
    v_lock = vlt_sub.add_parser("lock", help="lock a conversation with a passphrase")
    v_lock.add_argument("id")
    v_unlock = vlt_sub.add_parser("unlock", help="open a locked conversation in the server")
    v_unlock.add_argument("id")
    v_unlock.add_argument("--minutes", type=float, default=30, help="how long (default 30)")
    v_unlock.add_argument("--permanently", action="store_true", help="remove the lock")
    vlt_sub.add_parser("relock", help="close every unlocked conversation in the server now")

    rs = sub.add_parser("research", help="research jobs (run in the background by the server)")
    rs_sub = rs.add_subparsers(dest="research_command", metavar="ACTION", required=True)
    rv = rs_sub.add_parser("review", help="notes per paper + comparison table + references")
    rv.add_argument("library")
    rv.add_argument("--question", required=True, help="the review question")
    rv.add_argument("--style", choices=["apa", "ieee"], default="apa")
    rv.add_argument("--at", help="start later: HH:MM (local time) or an ISO date-time")
    rv.add_argument(
        "--files",
        nargs="+",
        metavar="PDF",
        help="review only these PDFs (in the library folder; indexed when the job starts)",
    )
    rv.add_argument("--files-from", type=Path, metavar="LIST",
                    help="like --files, read from a file with one path per line")  # fmt: skip
    rv.add_argument("--criteria", help="inclusion/exclusion criteria for screening, or @file")
    rv.add_argument("--no-screen", action="store_true",
                    help="read every paper in full (no title/abstract screening)")  # fmt: skip
    eng = rs_sub.add_parser("engine", help="optional engines (PaperQA2, citeproc-js, pandoc)")
    eng.add_argument("action", choices=["install", "status"])
    eng.add_argument("name", choices=["paperqa", "citeproc-js", "pandoc", "docling"])
    rsearch = rs_sub.add_parser("search", help="search OpenAlex, Semantic Scholar, arXiv, "
                                "Crossref, PubMed (opt-in per environment)")  # fmt: skip
    rsearch.add_argument("query")
    rsearch.add_argument("--env", help="environment (default: the library's, or litreview-online)")
    rsearch.add_argument("--library", help="use this library's environment")
    rsearch.add_argument("--source", action="append", dest="sources", metavar="NAME",
                         help="only this source (repeatable)")  # fmt: skip
    rsearch.add_argument("--limit", type=int, default=10, help="results per source (max 50)")
    rsearch.add_argument("--json", action="store_true")
    radd = rs_sub.add_parser("add", help="download open-access PDFs into a library and index them")
    radd.add_argument("library")
    radd.add_argument("ids", nargs="+", metavar="ID", help="doi:10.x/y, arxiv:2401.01234 or a DOI")
    rb = rs_sub.add_parser("ask", help="answer every question in a file (one per line)")
    rb.add_argument("library")
    rb.add_argument("file", type=Path)
    rb.add_argument("--at", help="start later: HH:MM (local time) or an ISO date-time")
    rb.add_argument(
        "--engine",
        choices=["lean", "paperqa"],
        default="lean",
        help="paperqa: the optional deep engine (slower; good for overnight)",
    )
    rb.add_argument("--evidence-k", type=int, default=10, help="paperqa: passages to weigh")
    rb.add_argument("--skip-summary", action="store_true", help="paperqa: no summaries")

    jb = sub.add_parser("jobs", help="research jobs: list, progress, results")
    jb_sub = jb.add_subparsers(dest="jobs_command", metavar="ACTION")
    jb_sub.add_parser("list", help="all jobs (default)")
    for name, text in (("show", "progress of one job"), ("cancel", "stop a job")):
        jb_sub.add_parser(name, help=text).add_argument("id")
    jres = jb_sub.add_parser("result", help="print a finished job's result")
    jres.add_argument("id")
    jres.add_argument("--bib", action="store_true", help="print the review's BibTeX instead")

    art = sub.add_parser("artifacts", help="versioned files of conversations, jobs and runs")
    art_sub = art.add_subparsers(dest="artifacts_command", metavar="ACTION")
    art_sub.add_parser("list", help="repositories (default)")
    art_actions = (
        ("files", "files in a repository"),
        ("log", "versions (of a file)"),
        ("show", "print a file"),
        ("restore", "bring back an old version"),
    )
    for name, text in art_actions:
        a = art_sub.add_parser(name, help=text)
        a.add_argument("scope", choices=["conversation", "job", "run"])
        a.add_argument("owner", help="conversation, job or run id")
        if name in ("log",):
            a.add_argument("path", nargs="?")
        if name in ("show", "restore"):
            a.add_argument("path")
        if name == "show":
            a.add_argument("--rev")
        if name == "restore":
            a.add_argument("rev")

    cite = sub.add_parser("cite", help="format references (APA 7, IEEE, or a .csl style)")
    cite.add_argument("file", type=Path, help="CSL-JSON (e.g. exported from Zotero)")
    cite.add_argument("--style", default="apa", help="apa (default), ieee, or a .csl file")
    cite.add_argument("--plain", action="store_true", help="no Markdown italics")
    cite.add_argument("--engine", choices=["auto", "builtin", "citeproc-js", "pandoc"],
                      help="CSL engine (default: [research] citation_engine, auto)")  # fmt: skip

    bke = sub.add_parser("backend", help="hosted provider templates for backends.toml")
    bke_sub = bke.add_subparsers(dest="backend_command", metavar="ACTION", required=True)
    bke_sub.add_parser("providers", help="the hosted providers with templates")
    bke_sub.add_parser("template", help="print a backends.toml block").add_argument("provider")
    mdl = sub.add_parser("models", help="model files: verified downloads through egress")
    mdl_sub = mdl.add_subparsers(dest="models_command", metavar="ACTION", required=True)
    pull = mdl_sub.add_parser("pull", help="download a GGUF/safetensors file and verify it")
    pull.add_argument("url")
    pull.add_argument("--sha256", required=True, help="the publisher's SHA-256 of the file")
    pull.add_argument("--name", help="file name (default: from the URL)")
    pull.add_argument("--env", help="whose network policy applies (default environment)")
    mdl_sub.add_parser("list", help="downloaded, verified model files")

    dsh = sub.add_parser("dashboard", help="open the dashboard (a single-use sign-in link)")
    dsh.add_argument("--no-open", action="store_true", help="only print the link")

    appr = sub.add_parser("approvals", help="answer requests Cairnward is waiting on (y/c/e/n)")
    appr_sub = appr.add_subparsers(dest="approvals_command", metavar="ACTION")
    appr_sub.add_parser("watch", help="wait for requests and answer them (default)")
    appr_list = appr_sub.add_parser("list", help="show pending requests and exit")
    appr_list.add_argument("--json", action="store_true", help="machine-readable output")
    appr_ans = appr_sub.add_parser("answer", help="answer one request by id")
    appr_ans.add_argument("id")
    appr_ans.add_argument("reply", choices=["y", "c", "e", "n"])
    appr.add_argument("--no-notify", action="store_true", help="no desktop notifications")

    st = sub.add_parser("status", help="is Cairnward running, and how")
    st.add_argument(
        "--wait",
        type=float,
        default=0,
        metavar="SECONDS",
        help="wait up to this long for Cairnward to come up",
    )

    bench = sub.add_parser("bench", help="model speed measurements")
    bench_sub = bench.add_subparsers(dest="bench_command", metavar="ACTION", required=True)
    bi = bench_sub.add_parser(
        "import", help="use results of scripts/bench_backend.py for waiting-time estimates"
    )
    bi.add_argument("files", nargs="+", type=Path)
    bench_sub.add_parser("show", help="the stored speeds")

    runs = sub.add_parser("runs", help="past `cairnward run` sessions: review and undo changes")
    runs_sub = runs.add_subparsers(dest="runs_command", metavar="ACTION", required=True)
    runs_sub.add_parser("list", help="runs with exit code and files changed")
    rd = runs_sub.add_parser("diff", help="what a run changed")
    rd.add_argument("run", help="run id (the first few characters are enough)")
    rd.add_argument("--stat", action="store_true", help="only a summary per file")
    rv = runs_sub.add_parser("revert", help="undo a run's changes")
    rv.add_argument("run")
    rv.add_argument(
        "--force", action="store_true", help="also undo files that changed again since the run"
    )
    rp = runs_sub.add_parser("prune", help="delete old snapshots")
    rp.add_argument("--keep", type=int, default=20, help="keep the newest N runs (default 20)")

    lib = sub.add_parser("library", help="literature libraries: folders of PDFs, indexed")
    lib_sub = lib.add_subparsers(dest="library_command", metavar="ACTION", required=True)
    lc = lib_sub.add_parser("create", help="register a folder of PDFs as a library")
    lc.add_argument("name")
    lc.add_argument("folder", type=Path)
    lc.add_argument("--env", default="litreview-offline", help="environment (privacy, models)")
    lset = lib_sub.add_parser("set", help="parsing options: --parser docling, --metadata grobid")
    lset.add_argument("name")
    lset.add_argument(
        "--parser",
        choices=["basic", "docling"],
        help="text extraction (docling: layout, tables, OCR; optional engine)",
    )
    lset.add_argument(
        "--metadata",
        choices=["pdf", "grobid"],
        help="metadata and reference lists (grobid: your GROBID service)",
    )
    ldr = lib_sub.add_parser("drive", help="mirror PDFs from Google Drive (or any rclone remote), "
                             "read-only, then index")  # fmt: skip
    ldr.add_argument("name")
    ldr.add_argument("--remote", required=True, help="rclone remote and folder, e.g. gdrive:Papers")
    ldr.add_argument("--preview", action="store_true", help="only show what would be copied")
    ldr.add_argument(
        "--prune",
        action="store_true",
        help="delete local copies of PDFs no longer on the remote (drive/ only)",
    )
    ldr.add_argument("--no-index", action="store_true")
    ldr.add_argument("--yes", action="store_true", help="answer the network question with yes")
    lz = lib_sub.add_parser("zotero", help="copy PDFs and metadata from Zotero (read-only, "
                            "local API), then index")  # fmt: skip
    lz.add_argument("name", help="the library to fill")
    lz.add_argument("--collection", help="a Zotero collection (e.g. Thesis or Thesis/Chapter 2); "
                    "default: the whole Zotero library")  # fmt: skip
    lz.add_argument("--url", default="http://127.0.0.1:23119/api", help="Zotero's local API")
    lz.add_argument(
        "--storage", type=Path, help="Zotero's storage folder (default ~/Zotero/storage)"
    )
    lz.add_argument("--no-index", action="store_true", help="only copy; index later")
    lz.add_argument("--list-collections", action="store_true", help="show Zotero's collections")
    li = lib_sub.add_parser("index", help="parse, chunk and embed new or changed PDFs")
    li.add_argument("name")
    li.add_argument("--prune", action="store_true", help="forget PDFs no longer in the folder")
    li.add_argument("--rebuild", action="store_true", help="re-index everything from scratch")
    lib_sub.add_parser("list", help="libraries and their size")
    ls_ = lib_sub.add_parser("show", help="documents in a library")
    ls_.add_argument("name")
    lq = lib_sub.add_parser("search", help="find passages (vector + keyword search)")
    lq.add_argument("name")
    lq.add_argument("query")
    lq.add_argument("-k", type=int, default=5, help="number of passages (default 5)")
    lq.add_argument("--json", action="store_true")
    la = lib_sub.add_parser("ask", help="answer a question from the library, citing pages")
    la.add_argument("name")
    la.add_argument("question")
    la.add_argument("-k", type=int, default=8, help="passages to give the model (default 8)")
    la.add_argument("--effort", help="effort level (default: the environment's)")
    la.add_argument("--json", action="store_true")
    la.add_argument("--engine", choices=["lean", "paperqa"], default="lean",
                    help="lean (default) or paperqa (optional deep engine; slower)")  # fmt: skip
    la.add_argument("--evidence-k", type=int, default=10, help="paperqa: passages to weigh")
    la.add_argument("--skip-summary", action="store_true",
                    help="paperqa: no per-passage summaries (faster)")  # fmt: skip
    lrf = lib_sub.add_parser("refs", help="CSL-JSON of the library's papers (from PDF metadata)")
    lrf.add_argument("name")
    lk = lib_sub.add_parser("check", help="check a draft's quotations and references")
    lk.add_argument("name")
    lk.add_argument("draft", type=Path, help="a Markdown or text file")
    lk.add_argument("--json", action="store_true")
    lr = lib_sub.add_parser("remove", help="delete a library's index (the PDFs stay)")
    lr.add_argument("name")

    sbx = sub.add_parser("sandbox", help="the OS sandbox (Anthropic sandbox-runtime)")
    sbx_sub = sbx.add_subparsers(dest="sandbox_command", metavar="ACTION", required=True)
    sbx_sub.add_parser("install", help="install the pinned sandbox-runtime (needs Node.js 22.12+)")
    sbx_sub.add_parser("status", help="where the sandbox is and whether it can be used")

    cli_cmd = sub.add_parser("client", help="API keys for chat clients and coding agents")
    cli_sub = cli_cmd.add_subparsers(dest="client_command", metavar="ACTION", required=True)
    add = cli_sub.add_parser("add", help="create (or rotate) a client's API key; shown once")
    add.add_argument("name")
    rm = cli_sub.add_parser("remove", help="revoke a client's API key")
    rm.add_argument("name")
    cli_sub.add_parser("list", help="clients with API keys")

    api = sub.add_parser("api", help="the gateway's API contract")
    api_sub = api.add_subparsers(dest="api_command", metavar="ACTION", required=True)
    oas = api_sub.add_parser("schema", help="write the OpenAPI document")
    oas.add_argument("--out", type=Path, default=Path("api/openapi.json"))
    oas.add_argument("--check", action="store_true", help="only check it is up to date")

    egress = sub.add_parser("egress", help="run cairnward-egress, the outbound proxy (foreground)")
    egress.add_argument("--env", help="environment for the printed session (default: default env)")
    egress.add_argument("--client", help="client name to attribute traffic to")
    egress.add_argument("--port", type=int, help="port (default: [server] egress_port)")

    store = sub.add_parser("store", help="local storage (vault and keys)")
    store_sub = store.add_subparsers(dest="store_command", metavar="ACTION", required=True)
    store_sub.add_parser("info", help="database files, schema versions and counts")

    priv = sub.add_parser("privacy", help="privacy settings")
    priv_sub = priv.add_subparsers(dest="privacy_command", metavar="ACTION", required=True)
    show = priv_sub.add_parser("show", help="effective privacy settings and where they come from")
    group = show.add_mutually_exclusive_group()
    group.add_argument("--env", help="show one environment (default: global settings)")
    group.add_argument("--all", action="store_true", help="one-line summary per environment")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _load(args: argparse.Namespace) -> Config:
    return load_config(default_paths(args.config_dir))


def _cmd_config_paths(args: argparse.Namespace) -> int:
    p = default_paths(args.config_dir)
    for label, path in [("config", p.config_dir), ("data", p.data_dir), ("state", p.state_dir)]:
        print(f"{label:7} {path}")
    return 0


def _cmd_config_check(args: argparse.Namespace) -> int:
    cfg = _load(args)
    files = ", ".join(str(f) for f in cfg.files_loaded) or "none (built-in defaults)"
    print(f"Configuration OK. Files read: {files}")
    print(f"Backends:     {', '.join(cfg.backends.backends)}")
    roles = ", ".join(f"{r}={m.model}@{m.backend}" for r, m in cfg.backends.roles.items())
    print(f"Models:       {roles}")
    print(f"Effort:       {', '.join(cfg.effort_levels_ordered())}")
    print(f"Environments: {', '.join(sorted(cfg.environments))}")
    print(f"Clients:      {', '.join(cfg.clients.clients) or '(none)'}")
    print(f"Privacy:      {cfg.privacy().label} (global)")

    from cairnward.audit import AuditLog, enabled_for, record_settings

    with AuditLog.open(cfg.paths, enabled_for(cfg)) as log:
        sync = record_settings(cfg, log)
    if sync.first_snapshot:
        print("Audit:        first settings snapshot recorded")
    if sync.enabled_after_gap_s is not None:
        print(f"Audit:        re-enabled after {sync.enabled_after_gap_s:.0f} s off (recorded)")
    if sync.changes:
        print(f"Audit:        {len(sync.changes)} settings change(s) recorded")
    if sync.disabled:
        print("Audit:        audit = off: recorded 'audit.disabled'; nothing more will be logged")
    return 0


def _cmd_config_schema(args: argparse.Namespace) -> int:
    if not args.check:
        for path in write_schemas(args.out):
            print(f"wrote {path}")
        return 0
    stale = []
    for filename, schema in schemas().items():
        path = args.out / filename
        if not path.exists() or path.read_text(encoding="utf-8") != render(schema):
            stale.append(str(path))
    if stale:
        print("Out of date (run `cairnward config schema`): " + ", ".join(stale), file=sys.stderr)
        return 1
    print("Schemas up to date.")
    return 0


def _cmd_store_info(args: argparse.Namespace) -> int:
    from cairnward.store import Vault

    paths = default_paths(args.config_dir)
    with Vault.open(paths) as vault:
        status = vault.schema_status()
        for name, path in Vault.files(paths).items():
            size = path.stat().st_size if path.exists() else 0
            rev, head = status[name]
            state = "up to date" if rev == head else f"behind (head {head})"
            print(f"{name:6} {path}  {size / 1024:.0f} KiB  schema {rev} ({state})")
        for key, value in vault.stats().items():
            print(f"{key:14} {value}")
    return 0


def _parse_expect(values: list[str]) -> list[tuple[int, str]]:
    out = []
    for v in values:
        seq, _, h = v.partition(":")
        if not seq.isdigit() or len(h) != 64:
            raise SystemExit(f"--expect needs SEQ:HASH as printed by `audit head` (got {v!r})")
        out.append((int(seq), h))
    return out


def _cmd_audit(args: argparse.Namespace) -> int:
    from cairnward.audit import AuditLog

    paths = default_paths(args.config_dir)
    with AuditLog.open(paths) as log:
        if args.audit_command == "head":
            seq, h = log.head()
            print(f"{seq}:{h}")
            return 0
        if args.audit_command == "anchor":
            return _audit_anchor(args, log)
        if args.audit_command == "verify":
            res = log.verify(_parse_expect(args.expect))
            for note in res.notes:
                print(f"note: {note}")
            if res.ok:
                print(f"OK: {res.records} records, chain and mirror intact.")
                print(f"Head {res.head_seq}:{res.head_hash}")
                return _audit_verify_anchors(args, log) if args.anchors else 0
            print(
                f"FAILED: {len(res.problems)} problem(s) in {res.records} records", file=sys.stderr
            )
            for p in res.problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        from cairnward.audit.egress_report import parse_since, record_time

        since = parse_since(args.since) if args.since else None
        if args.audit_command == "egress":
            from cairnward.audit import egress_report as er

            cfg = _load(args)
            remote = {n for n, b in cfg.backends.backends.items() if b.location != "local"}
            rows = er.build(log.records(), remote_backends=remote, run=args.run,
                            client=args.client, environment=args.env,
                            conversation=args.conversation, since=since)  # fmt: skip
            render = er.render_json if args.json else er.render_csv if args.csv else er.render_text
            sys.stdout.write(render(rows))
            return 0
        filtered = any((args.run, args.client, args.env, since))
        records = log.records(action=args.action, conversation_id=args.conversation,
                              last=None if filtered else args.last)  # fmt: skip
        if filtered:
            records = [
                r for r in records
                if (not args.run or (r.run_id or "").startswith(args.run))
                and (not args.client or r.client == args.client)
                and (not args.env or r.environment == args.env)
                and (not since or record_time(r) >= since)
            ][-args.last:]  # fmt: skip
        for r in records:
            if args.json:
                print(r.to_json_line())
                continue
            bits = [r.phase, r.decision or "", r.outcome or "", r.target or ""]
            where = " ".join(b for b in bits if b)
            scope = f" [{r.environment}]" if r.environment else ""
            print(f"{r.seq:>6} {r.ts} {r.actor:16} {r.action:20} {where}{scope}")
    return 0


def _audit_anchor(args: argparse.Namespace, log: Any) -> int:
    from cairnward.audit.anchor import Anchorer, AnchorError

    cfg = _load(args)
    anchorer = Anchorer(cfg.paths, cfg.main.anchor)
    try:
        if args.setup:
            keys = anchorer.setup()
            print(
                "1. Create a PRIVATE repository you own (e.g. github.com/<you>/cairnward-anchors)."
            )
            print("2. Add this as a deploy key WITH write access (that repo only):")
            print(f"   {keys['deploy']}")
            print("3. Put the repo in cairnward.toml:")
            print('   [anchor]\n   repo = "git@github.com:<you>/cairnward-anchors.git"')
            print('   and set audit_anchor = "git" under [privacy] (Strict has it on).')
            print("4. Run `cairnward audit anchor` once to check it works.")
            print(f"Signing key (also stored in the repo): {keys['sign']}")
            return 0
        payload = anchorer.anchor(log)
    except AnchorError as exc:
        log.append("audit.anchor", actor="user", outcome="error",
                   details={"error": str(exc)[:300]})  # fmt: skip
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    if payload is None:
        print("nothing to anchor yet" if log.head()[0] == 0
              else "already anchored: no new records since the last checkpoint")  # fmt: skip
        return 0
    log.append("audit.anchor", actor="user", outcome="ok", target=cfg.main.anchor.repo,
               details={"seq": payload["seq"], "hash": payload["hash"]})  # fmt: skip
    print(f"anchored seq {payload['seq']} ({payload['hash'][:16]}...) to {cfg.main.anchor.repo}")
    return 0


def _audit_verify_anchors(args: argparse.Namespace, log: Any) -> int:
    from cairnward.audit.anchor import Anchorer, AnchorError

    cfg = _load(args)
    try:
        res = Anchorer(cfg.paths, cfg.main.anchor).verify(log)
    except AnchorError as exc:
        print(f"anchors: {exc}", file=sys.stderr)
        return 1
    if res.ok:
        print(f"anchors OK: {res.anchors} checkpoint(s), last seq {res.last_seq} at {res.last_ts}; "
              f"{res.unanchored} record(s) since then")  # fmt: skip
        return 0
    print(f"anchors FAILED: {len(res.problems)} problem(s)", file=sys.stderr)
    for problem in res.problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


def _cmd_policy_explain(args: argparse.Namespace) -> int:
    from typing import get_args

    from cairnward.policy import Action, PolicyEngine
    from cairnward.policy.engine import Kind

    if args.kind not in get_args(Kind):
        print(
            f"Unknown action kind '{args.kind}'. Known: {', '.join(get_args(Kind))}",
            file=sys.stderr,
        )
        return 2
    cfg = _load(args)
    env = args.env or cfg.main.defaults.environment
    if env not in cfg.environments:
        print(f"Unknown environment '{env}'", file=sys.stderr)
        return 2
    engine = PolicyEngine(cfg)
    action = Action(kind=args.kind, target=args.target, environment=env, initiated_by=args.by)
    rule = engine.rule_for(action)
    decision = engine.check(action, dry_run=True)
    would_ask = decision.reason.endswith("(would ask)")
    outcome = "ASK" if would_ask else decision.decision.upper()
    print(f"{outcome}  ({decision.risk} risk) in environment '{env}'")
    print(f"  rule:   {rule.reason}")
    if not would_ask and decision.reason != rule.reason:
        print(f"  result: {decision.reason}")
    if decision.advisory:
        print("  note:   isolation is off, so the OS doesn't enforce this decision")
    print(f"  privacy: {cfg.privacy(env).label}")
    return 0


def _cmd_egress(args: argparse.Namespace) -> int:
    import asyncio

    from cairnward.audit import AuditLog, enabled_for, record_settings
    from cairnward.egress import EgressProxy, Session, Sessions
    from cairnward.policy import DenyApprover, PolicyEngine, TerminalApprover

    cfg = _load(args)
    env = args.env or cfg.main.defaults.environment
    if env not in cfg.environments:
        print(f"Unknown environment '{env}'", file=sys.stderr)
        return 2
    approver = TerminalApprover() if sys.stdin.isatty() else DenyApprover()
    with AuditLog.open(cfg.paths, enabled_for(cfg)) as log:
        record_settings(cfg, log)
        engine = PolicyEngine(cfg, approver=approver, audit=log)
        sessions = Sessions()
        token = sessions.create(Session(environment=env, client=args.client, actor="user"))
        proxy = EgressProxy(cfg, engine, sessions, port=args.port)

        async def run() -> None:
            port = await proxy.start()
            print(
                f"cairnward-egress listening on 127.0.0.1:{port} "
                f"(environment '{env}', {cfg.privacy(env).label})",
                file=sys.stderr,
            )
            print("Use this proxy setting (keep the token private):", file=sys.stderr)
            print(f"  export HTTPS_PROXY=http://cw:{token}@127.0.0.1:{port}", file=sys.stderr)
            print(f"  export HTTP_PROXY=http://cw:{token}@127.0.0.1:{port}", file=sys.stderr)
            if not sys.stdin.isatty():
                print("No terminal: requests that need approval are denied.", file=sys.stderr)
            await proxy.serve_forever()

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("\nstopped", file=sys.stderr)
    return 0


def _cmd_client(args: argparse.Namespace) -> int:
    from cairnward.gateway import ClientKeys

    keys = ClientKeys.at(default_paths(args.config_dir).state_dir)
    if args.client_command == "add":
        try:
            key = keys.add(args.name)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"API key for '{args.name}' (shown once; store it in the client's settings):")
        print(key)
        cfg = default_paths(args.config_dir)
        print(
            f"Base URL: http://127.0.0.1:<gateway_port>/v1  (see {cfg.main_config})",
            file=sys.stderr,
        )
        return 0
    if args.client_command == "remove":
        if keys.remove(args.name):
            print(f"Revoked '{args.name}'.")
            return 0
        print(f"No key for '{args.name}'.", file=sys.stderr)
        return 1
    for name, created in keys.names().items():
        print(f"{name:24} created {created}")
    return 0


def _openapi_text() -> str:
    from cairnward.gateway.app import openapi_document

    return json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"


def _cmd_api(args: argparse.Namespace) -> int:
    text = _openapi_text()
    if args.check:
        if not args.out.exists() or args.out.read_text(encoding="utf-8") != text:
            print(f"{args.out} is out of date (run `cairnward api schema`)", file=sys.stderr)
            return 1
        print("OpenAPI document up to date.")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _cmd_serve(args: argparse.Namespace) -> int:
    from cairnward.core.server import serve

    cfg = _load(args)
    if not sys.stdin.isatty():
        _log("No terminal: requests that need approval are denied.")
    try:
        return serve(cfg, with_egress=not args.no_egress, log=_log)
    except KeyboardInterrupt:
        _log("\nstopped")
        return 0


def _cmd_core(args: argparse.Namespace) -> int:
    from cairnward.core.server import run_core

    run_core(_load(args), egress_control=args.egress_control, log=_log)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from cairnward.run import RunError, run_agent

    command = args.agent_command
    command = command[1:] if command[:1] == ["--"] else command
    try:
        return run_agent(
            _load(args),
            command=command,
            environment=args.env,
            profile_name=args.profile,
            model=args.model,
            workspace=args.workspace,
            client=args.client,
            log=_log,
            budget={
                "max_requests": args.max_requests,
                "max_tokens": args.max_tokens,
                "max_minutes": args.max_minutes,
            },
        )
    except RunError as exc:
        _log(f"cairnward run: {exc}")
        return 2


def _print_checks(checks: list) -> int:  # type: ignore[type-arg]
    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "--  "}
    for c in checks:
        print(f"[{marks[c.status]}] {c.name:22} {c.detail}")
        if c.fix and c.status in ("warn", "fail"):
            print(f"       {'':22} fix: {c.fix}")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    print(f"\n{fails} problem(s), {warns} warning(s).")
    return 1 if fails else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from cairnward.doctor import run_checks

    checks = run_checks(default_paths(args.config_dir))
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
        return 1 if any(c.status == "fail" for c in checks) else 0
    return _print_checks(checks)


def _cmd_setup(args: argparse.Namespace) -> int:
    from cairnward.doctor import run_checks
    from cairnward.sandbox import SandboxUnavailable, install, locate
    from cairnward.setup import setup

    paths = default_paths(args.config_dir)
    ask = input if sys.stdin.isatty() else None
    try:
        preset = setup(paths, preset=args.preset, ask=ask, say=print)
    except ValueError as exc:
        _log(f"cairnward setup: {exc}")
        return 2
    cfg = load_config(paths)
    from cairnward.audit import AuditLog, enabled_for, record_settings

    with AuditLog.open(cfg.paths, enabled_for(cfg)) as log:
        record_settings(cfg, log)
    needs_sandbox = preset != "open" and locate(paths) is None

    def wants_install() -> bool:
        if args.install_sandbox:
            return True
        reply = ask("Install the sandbox now? [Y/n] ") if ask else "n"
        return reply.strip().lower() in ("", "y", "yes")

    if needs_sandbox and wants_install():
        try:
            print(f"Installed: {install(paths)}")
        except (SandboxUnavailable, OSError) as exc:
            print(f"Sandbox not installed: {exc}")
    print("\nChecking prerequisites (cairnward doctor):\n")
    code = _print_checks(run_checks(paths))
    print(
        "\nNext: `cairnward client add <name>` for each chat app or agent, then `cairnward serve`."
    )
    return code


def _cmd_status(args: argparse.Namespace) -> int:
    import time

    import httpx

    from cairnward.run.runner import RunError, management

    cfg = _load(args)
    deadline = time.monotonic() + args.wait
    while True:
        try:
            with management(cfg) as mgmt:
                st = mgmt.get("/v1/status").json()
            break
        except (RunError, httpx.HTTPError) as exc:
            if time.monotonic() >= deadline:
                print(f"not running ({exc})")
                return 1
            time.sleep(0.25)
    mode = (
        "core isolated in the sandbox" if st["isolated"] else "single process (core not isolated)"
    )
    print(f"running: cairnward {st['version']}, {mode}")
    print(f"gateway: http://127.0.0.1:{st['gateway_port']}/v1")
    print(f"egress:  {'127.0.0.1:' + str(st['egress_port']) if st['egress_port'] else 'off'}")
    print(f"privacy: {st['privacy']} (global); active runs: {st['active_runs']}")
    return 0


def _cmd_approvals(args: argparse.Namespace) -> int:
    from cairnward import approvals_cli as ac
    from cairnward.run.runner import RunError, management

    cfg = _load(args)
    try:
        mgmt = management(cfg)
    except RunError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    with mgmt:
        action = args.approvals_command or "watch"
        if action == "list":
            return ac.list_pending(mgmt, sys.stdout, as_json=args.json)
        if action == "answer":
            granted, scope = ac.ANSWERS[args.reply]
            if ac.send(mgmt, args.id, granted, scope):
                return 0
            print(f"no pending request {args.id} (answered or timed out)", file=sys.stderr)
            return 1
        try:
            return ac.watch(mgmt, sys.stdin, sys.stdout, notify=not args.no_notify)
        except KeyboardInterrupt:
            return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    import os

    action = args.mcp_command or "connect"
    cfg = _load(args)
    if action == "connect":
        from cairnward.mcp.shim import run_shim

        key = args.key or os.environ.get("CAIRNWARD_API_KEY")
        if not key:
            print("cairnward mcp: no API key (--key or CAIRNWARD_API_KEY; "
                  "`cairnward client add <name>` makes one)", file=sys.stderr)  # fmt: skip
            return 2
        url = args.url or os.environ.get("CAIRNWARD_MCP_URL") or (
            f"http://127.0.0.1:{cfg.main.server.gateway_port}/mcp"
            + (f"/{args.env}" if args.env else ""))  # fmt: skip
        return run_shim(url, key, sys.stdin, sys.stdout)

    from cairnward.run.runner import RunError, management

    try:
        mgmt = management(cfg)
    except RunError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    mgmt.timeout = httpx_timeout()
    with mgmt:
        if action == "list":
            servers = mgmt.get("/v1/mcp/servers").json()
            if not servers:
                print("no MCP servers configured (mcp.toml)")
            for s in servers:
                p = s["permissions"]
                print(f"{s['name']}  {s['version'] or '(no version noted)'}  {s['description']}")
                print(f"  command : {' '.join(s['command'])}")
                print(f"  enabled : {', '.join(s['environments']) or 'nowhere (mcp_servers)'}")
                print(f"  network : {', '.join(p['network']) or 'none'}")
                print(f"  write   : {', '.join(p['write']) or '{data} only'}")
                read = "everything except secrets" if p["read"] is None else ", ".join(p["read"])
                print(f"  read    : {read}")
            return 0
        params = {"environment": args.env} if args.env else {}
        resp = mgmt.get(f"/v1/mcp/{args.server}/tools", params=params)
        if resp.status_code != 200:
            print(f"cairnward: {resp.json().get('detail', resp.text)}", file=sys.stderr)
            return 1
        data = resp.json()
        tools = data["tools"]
        for t in tools:
            mark = {"approved": "ok", "new": "NEW", "changed": "CHANGED"}[t["status"]]
            print(f"[{mark:7}] {t['name']}{'' if t['offered'] else '  (not offered)'}")
            if (action == "approve" and t["status"] != "approved") or (
                action == "tools" and t["flags"]
            ):
                print("    " + (t["description"] or "(no description)").replace("\n", "\n    "))
                if action == "approve":
                    schema = json.dumps(t["input_schema"], ensure_ascii=False)
                    print(f"    input: {schema[:400]}")
            for flag in t["flags"]:
                print(f"    WARNING: {flag}")
        if action == "tools":
            return 0
        todo = {t["name"]: t["hash"] for t in tools if t["status"] != "approved"}
        if not todo:
            print("all tools already approved")
            return 0
        if not args.yes:
            print(f"Approve these {len(todo)} definition(s) for '{args.server}'? [y/N] ", end="")
            sys.stdout.flush()
            if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                print("not approved")
                return 1
        resp = mgmt.post(f"/v1/mcp/{args.server}/approve",
                         json={"environment": data["environment"], "tools": todo})  # fmt: skip
        if resp.status_code != 200:
            print(f"cairnward: {resp.json().get('detail', resp.text)}", file=sys.stderr)
            return 1
        print(f"approved: {', '.join(resp.json()['approved'])}")
        return 0


def httpx_timeout() -> Any:
    import httpx

    return httpx.Timeout(180, connect=10)  # starting a server can take a while (npx, uvx)


def _cmd_models(args: argparse.Namespace) -> int:
    from urllib.parse import urlsplit

    from cairnward import model_files as mf
    from cairnward.audit import AuditLog
    from cairnward.run.runner import RunError, management

    cfg = _load(args)
    dest = mf.models_dir(cfg.paths.data_dir)
    if args.models_command == "list":
        items = mf.listed(dest)
        if not items:
            print("no verified model files yet (cairnward models pull)")
        for name, info in items.items():
            gb = int(info["size"]) / 1024**3
            print(f"{name}  {gb:.2f} GB  sha256 {str(info['sha256'])[:16]}...")
            print(f"    {dest / name}")
        return 0
    env = args.env or cfg.main.defaults.environment
    try:
        mgmt = management(cfg)
    except RunError as exc:
        print(f"cairnward: {exc} (downloads go through cairnward-egress)", file=sys.stderr)
        return 1
    with mgmt:
        resp = mgmt.post("/v1/egress/session", json={"environment": env})
        if resp.status_code != 200:
            print(f"cairnward: {resp.json().get('detail', resp.text)}", file=sys.stderr)
            return 1
        sess = resp.json()
        proxy = f"http://cw:{sess['token']}@127.0.0.1:{sess['port']}"
        print(
            f"Downloading through cairnward-egress ('{env}' network policy). If a host needs "
            "approval, answer in `cairnward approvals` or the dashboard.",
            file=sys.stderr,
        )
        last = [0]

        def progress(done: int, total: int | None) -> None:
            if done - last[0] >= 256 * 1024**2 or (total and done == total):
                last[0] = done
                pct = f" ({done * 100 // total}%)" if total else ""
                print(f"  {done / 1024**3:.2f} GB{pct}", file=sys.stderr)

        outcome, detail = "ok", ""
        try:
            got = mf.download(args.url, args.sha256, dest, name=args.name, proxy=proxy,
                              progress=progress)  # fmt: skip
        except mf.DownloadError as exc:
            outcome, detail, got = "error", str(exc), None
        finally:
            mgmt.post("/v1/egress/session/end", json={"session": sess["session"]})
    with AuditLog.open(cfg.paths) as log:
        log.append("model.download", actor="user", target=urlsplit(args.url).netloc,
                   environment=env, outcome=outcome,
                   details={"file": args.name or urlsplit(args.url).path.rsplit("/", 1)[-1][:200],
                            "sha256": args.sha256.lower(), "size": got.size if got else None,
                            "error": detail[:300] or None})  # fmt: skip
    if got is None:
        print(f"cairnward: {detail}", file=sys.stderr)
        return 1
    print(f"verified and saved: {got.path} ({got.size / 1024**3:.2f} GB)")
    if got.path.suffix == ".gguf":
        print(
            "Use it with llama.cpp (llama-server -m <path>), or in Ollama: write a Modelfile "
            f"containing 'FROM {got.path}' and run `ollama create <name> -f Modelfile`."
        )
    return 0


def parse_at(value: str, now: Any = None) -> str:
    """'23:30' (local, today or tomorrow) or an ISO date-time -> ISO UTC."""
    from datetime import UTC, datetime, timedelta

    now = now or datetime.now().astimezone()
    if len(value) <= 5 and ":" in value:
        hh, mm = (int(x) for x in value.split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
    else:
        at = datetime.fromisoformat(value)
        if at.tzinfo is None:
            at = at.astimezone()
    return str(at.astimezone(UTC).isoformat(timespec="seconds"))


def _research_search(args: argparse.Namespace, mgmt: Any) -> int:
    body = {"query": args.query, "environment": args.env, "library": args.library,
            "sources": args.sources, "limit": args.limit}  # fmt: skip
    resp = mgmt.post("/v1/research/search", json=body, timeout=180)
    if resp.status_code != 200:
        print(f"cairnward: {resp.json().get('detail', resp.text)}", file=sys.stderr)
        return 1
    out = resp.json()
    if args.json:
        print(json.dumps(out, indent=1))
        return 0
    for name, rep in out["sources"].items():
        if "error" in rep:
            print(f"  {name}: {rep['error']}", file=sys.stderr)
    for n, r in enumerate(out["records"], 1):
        who = ", ".join(r["authors"][:3]) + (" et al." if len(r["authors"]) > 3 else "")
        ident = (f"doi:{r['doi']}" if r["doi"] else f"arxiv:{r['arxiv']}" if r["arxiv"]
                 else f"{r['source']}:{r['id']}")  # fmt: skip
        extra = " · ".join(x for x in (
            "open-access PDF" if r["pdf_url"] else "", f"cited {r['cited_by']}"
            if r["cited_by"] is not None else "", r["source"]) if x)  # fmt: skip
        print(f"{n:2}. {who} ({r['year'] or 'n.d.'}). {r['title']}"
              + (f". {r['venue']}" if r["venue"] else ""))  # fmt: skip
        print(f"    {ident}  [{extra}]")
    if not out["records"]:
        print("no results")
    if out.get("warning"):
        print(f"note: {out['warning']}", file=sys.stderr)
    print(f"(environment {out['environment']}; add papers with: cairnward research add "
          "<library> <id> ...)", file=sys.stderr)  # fmt: skip
    return 0


def _research_add(args: argparse.Namespace, cfg: Config, mgmt: Any) -> int:
    import base64

    from cairnward.research import LibraryError, Research

    try:
        with Research.open(cfg.paths) as research:
            folder = research.get(args.library).folder
    except LibraryError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    resp = mgmt.post("/v1/research/fetch", json={"library": args.library, "ids": args.ids},
                     timeout=600)  # fmt: skip
    if resp.status_code != 200:
        print(f"cairnward: {resp.json().get('detail', resp.text)}", file=sys.stderr)
        return 1
    target = folder / "online"
    saved = 0
    for item in resp.json()["items"]:
        if "error" in item:
            print(f"  {item['id']}: {item['error']}", file=sys.stderr)
            continue
        target.mkdir(parents=True, exist_ok=True)
        pdf = target / Path(item["filename"]).name
        pdf.write_bytes(base64.b64decode(item["pdf_b64"]))
        pdf.with_name(pdf.name + ".csl.json").write_text(json.dumps(item["csl"], indent=1),
                                                         encoding="utf-8")  # fmt: skip
        print(f"  added {pdf.relative_to(folder)}: {item['title'][:70]}")
        saved += 1
    if saved:
        print("indexing ...")
        return main(["--config-dir", str(cfg.paths.config_dir), "library", "index", args.library])
    return 1


def _cmd_jobs(args: argparse.Namespace) -> int:
    from cairnward.run.runner import RunError, management

    cfg = _load(args)
    if args.command == "research" and args.research_command == "engine":
        import subprocess

        from cairnward.research import csl
        from cairnward.research.engines import paperqa

        if args.name == "citeproc-js":
            if args.action == "install":
                try:
                    lib = csl.install_citeproc_js(cfg.paths)
                except (csl.CslError, subprocess.CalledProcessError) as exc:
                    print(f"cairnward: {exc}", file=sys.stderr)
                    return 1
                print(f"citeproc-js installed ({lib}); with citation_engine = \"auto\" it now "
                      "formats references")  # fmt: skip
                return 0
            found = csl.citeproc_js(cfg.paths)
            print(f"citeproc-js: {'installed (' + str(found) + ')' if found else 'not installed'}")
            return 0 if found else 1
        if args.name == "docling":
            from cairnward.research.engines import docling

            if args.action == "install":
                print("Installing Docling with CPU-only PyTorch and its models (about 3 GB). The "
                      "exact versions and hashes are recorded on this first install.")  # fmt: skip
                try:
                    record = docling.install(cfg.paths)
                except (docling.DoclingError, subprocess.CalledProcessError) as exc:
                    print(f"cairnward: {exc}", file=sys.stderr)
                    return 1
                print(f"Docling installed; lock sha256 {record['lock_sha256'][:16]}..., models "
                      f"sha256 {record['models_sha256'][:16]}...")  # fmt: skip
                return 0
            exe = docling.python(cfg.paths)
            problems = docling.verify(cfg.paths) if exe else []
            print(f"docling: {'installed' if exe else 'not installed'}"
                  + "".join(f"; {p}" for p in problems))  # fmt: skip
            return 0 if exe and not problems else 1
        if args.name == "pandoc":
            pandoc_exe = csl.pandoc()
            if args.action == "install":
                print("pandoc isn't installed by Cairnward: install it with your package "
                      "manager (e.g. sudo apt install pandoc); version 2.11 or newer")  # fmt: skip
            print(f"pandoc: {'found (' + pandoc_exe + ')' if pandoc_exe else 'not found'}")
            return 0 if pandoc_exe else 1
        if args.action == "install":
            print("Installing PaperQA2 (about 250 MB, hash-checked) in its own environment...")
            paperqa.install(cfg.paths)
            return 0
        exe = paperqa.python(cfg.paths)
        print(f"paperqa: {'installed (' + str(exe) + ')' if exe else 'not installed'}")
        return 0 if exe else 1
    try:
        mgmt = management(cfg)
    except RunError as exc:
        print(f"cairnward: {exc} (jobs run inside the server)", file=sys.stderr)
        return 1

    def fail(resp: Any) -> int:
        print(f"cairnward: {resp.json().get('detail', resp.text)}", file=sys.stderr)
        return 1

    def line(j: dict[str, Any]) -> str:
        eta = f", about {j['eta_s'] // 60} min left" if j.get("eta_s") else ""
        when = (
            f", starts {j['not_before']}" if j.get("not_before") and j["status"] == "queued" else ""
        )
        engine = " (paperqa)" if j.get("engine") == "paperqa" else ""
        return (f"{j['id']}  {j['kind']:6} {j['status']:9} {j['done']}/{j['total']}  "
                f"{j['library']}{engine}{when}{eta}")  # fmt: skip

    with mgmt:
        if args.command == "research" and args.research_command == "search":
            return _research_search(args, mgmt)
        if args.command == "research" and args.research_command == "add":
            return _research_add(args, cfg, mgmt)
        if args.command == "research":
            start = parse_at(args.at) if args.at else None
            body: dict[str, Any] = {"library": args.library, "not_before": start}
            if args.research_command == "review":
                criteria = args.criteria
                if criteria and criteria.startswith("@"):
                    criteria = Path(criteria[1:]).read_text(encoding="utf-8").strip()
                body |= {"kind": "review", "question": args.question, "style": args.style,
                         "criteria": criteria, "screen": not args.no_screen}  # fmt: skip
                files = list(args.files or [])
                if args.files_from:
                    files += [x.strip() for x in args.files_from.read_text(
                        encoding="utf-8").splitlines() if x.strip()]  # fmt: skip
                if args.files or args.files_from:
                    # absolute here: the server's working directory isn't ours
                    body["files"] = [
                        str(Path(f).expanduser().absolute()) if Path(f).expanduser().exists() else f
                        for f in files
                    ]
            else:
                questions = args.file.read_text(encoding="utf-8").splitlines()
                body |= {"kind": "ask", "questions": [q for q in questions if q.strip()],
                         "engine": args.engine, "evidence_k": args.evidence_k,
                         "skip_summary": args.skip_summary}  # fmt: skip
            resp = mgmt.post("/v1/jobs", json=body)
            if resp.status_code != 200:
                return fail(resp)
            job = resp.json()
            print(f"queued: {line(job)}")
            if job.get("not_found"):
                print("not in the library folder yet (reviewed only if there when the job "
                      "starts): " + ", ".join(job["not_found"]))  # fmt: skip
            print(f"follow it with: cairnward jobs show {job['id']}")
            return 0
        action = args.jobs_command or "list"
        if action == "list":
            jobs = mgmt.get("/v1/jobs").json()
            if not jobs:
                print("no research jobs")
            for j in jobs:
                print(line(j))
            return 0
        if action == "cancel":
            resp = mgmt.post(f"/v1/jobs/{args.id}/cancel")
            if resp.status_code != 200:
                return fail(resp)
            print("cancelled" if resp.json()["cancelled"] else "already finished")
            return 0
        resp = mgmt.get(f"/v1/jobs/{args.id}")
        if resp.status_code != 200:
            return fail(resp)
        job = resp.json()
        if action == "result":
            if not job.get("result"):
                print(f"not finished yet: {line(job)}", file=sys.stderr)
                return 1
            path = Path(job["result"])
            if args.bib:
                path = path.with_name("references.bib")
                if not path.exists():
                    print("this job has no BibTeX (only reviews do)", file=sys.stderr)
                    return 1
            sys.stdout.write(path.read_text(encoding="utf-8"))
            return 0
        print(line(job))
        for st in job["steps"]:
            mark = {"done": "ok ", "failed": "BAD", "pending": "   "}[st["status"]]
            took = f" ({st['seconds']:.0f}s)" if st["seconds"] else ""
            err = f"  {st['error']}" if st["error"] else ""
            print(f"  [{mark}] {st['name']}: {(st['label'] or '')[:70]}{took}{err}")
        if job.get("result"):
            print(f"result: {job['result']}")
        return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    import tempfile

    from cairnward import backup

    cfg = _load(args)
    action = args.backup_command
    try:
        if action == "init":
            password = backup.init(cfg)
            if password:
                print(f"Backup repository created at {backup.repository(cfg)}.")
                print(f"Its password is in {backup.password_file(cfg)} (owner-only). Keep a copy"
                      " somewhere safe (a password manager): without it the backups can't be"
                      " restored.")  # fmt: skip
                print(f"  password: {password}")
            elif cfg.main.backup.password_command:
                print("Backup repository created (password from password_command).")
            if cfg.main.backup.mode == "none":
                print('Set [backup] mode = "on_demand" or "auto" in cairnward.toml to use it.')
            return 0
        if action == "run":
            if cfg.main.backup.mode == "none":
                print('cairnward: backups are off ([backup] mode = "none")', file=sys.stderr)
                return 1
            r = backup.run(cfg)
            print(f"snapshot {r['snapshot']}: {r['conversations']} conversations, "
                  f"{r['files']} files, {(r['bytes_added'] or 0) // 1024} KiB new, "
                  f"{r['seconds']:.0f}s")  # fmt: skip
            return 0
        if action == "list":
            for snap in backup.snapshots(cfg):
                print(
                    f"{snap['short_id']}  {snap['time'][:19].replace('T', ' ')}  "
                    f"{snap.get('summary', {}).get('total_files_processed', '?')} files"
                )
            print("(each holds the conversations, library and artifacts as they were)")
            return 0
        if action == "status":
            st = backup.status(cfg)
            print(f"mode: {st['mode']}")
            print(
                f"repository: {st['repository'] or '(not set)'}"
                + ("" if st.get("initialised") else "  (not set up: cairnward backup init)")
            )
            print(f"restic: {st['restic'] or 'not installed (e.g. sudo apt install restic)'}")
            if "snapshots" in st:
                print(f"snapshots: {st['snapshots']}")
            last = st["last"]
            print(
                f"last backup: {last['time'] + ' (' + last['snapshot'] + ')' if last else 'never'}"
            )
            if st["pending_purges"]:
                print(f"pending removal from backups: {st['pending_purges']} purged "
                      "conversation(s) (done on the next run)")  # fmt: skip
            for key in ("cloud_warning", "problem"):
                if st.get(key):
                    print(f"note: {st[key]}")
            return 0
        if action == "restore":
            if args.to:
                snap, folder = backup.restore(cfg, args.to, snapshot=args.snapshot, at=args.at)
                print(f"backup {snap['short_id']} ({snap['time'][:19]}) extracted to {folder}")
                return 0
            with tempfile.TemporaryDirectory(prefix="cw-restore-") as tmp:
                snap, folder = backup.restore(cfg, Path(tmp), snapshot=args.snapshot, at=args.at)
                counts = backup.import_conversations(cfg, folder)
            print(f"from backup {snap['short_id']} ({snap['time'][:19]}): "
                  f"{counts['added']} conversation(s) brought back, {counts['skipped']} already "
                  "here (left as they are)")  # fmt: skip
            return 0
    except backup.BackupError as exc:
        print(f"cairnward backup: {exc}", file=sys.stderr)
        return 1
    return 2


def _cmd_artifacts(args: argparse.Namespace) -> int:
    from cairnward.artifacts import ArtifactError, ArtifactStore

    store = ArtifactStore(_load(args).paths.data_dir)
    action = args.artifacts_command or "list"
    try:
        if action == "list":
            repos = store.repos()
            if not repos:
                print("no artifacts yet")
            for scope, owner, n in repos:
                print(f"{scope:12} {owner}  {n} file(s)")
        elif action == "files":
            print("\n".join(store.files(args.scope, args.owner)) or "(empty)")
        elif action == "log":
            for v in store.log(args.scope, args.owner, args.path):
                print(f"{v.rev[:12]}  {v.date[:19]}  {v.message}")
        elif action == "show":
            sys.stdout.write(store.read(args.scope, args.owner, args.path, args.rev)
                             .decode("utf-8", "replace"))  # fmt: skip
        else:
            rev = store.restore(args.scope, args.owner, args.path, args.rev)
            print(f"restored {args.path} as new version {rev[:12]}")
    except ArtifactError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    import webbrowser

    from cairnward.run.runner import RunError, management

    cfg = _load(args)
    try:
        with management(cfg) as mgmt:
            resp = mgmt.post("/v1/dashboard/link")
    except RunError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    if resp.status_code != 200:
        print(f"cairnward: {resp.json().get('detail', resp.text)}", file=sys.stderr)
        return 1
    url = resp.json()["url"]
    print("Sign-in link (works once, for 2 minutes):")
    print(url)
    if not args.no_open:
        webbrowser.open(url)
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    from cairnward.gateway.scheduling import BenchProfile

    profile = BenchProfile.at(default_paths(args.config_dir).state_dir)
    if args.bench_command == "import":
        try:
            models = profile.import_results(args.files)
        except (OSError, ValueError, KeyError) as exc:
            _log(f"cairnward bench import: {exc}")
            return 1
        print(f"Imported speeds for: {', '.join(models)} (used after the next `cairnward serve`).")
    for model, s in sorted(profile.speeds.items()):
        gen = f"{s.gen_tps:.1f}" if s.gen_tps else "?"
        print(f"{model:24} reads {s.prompt_tps:6.1f} tok/s   writes {gen} tok/s")
    if not profile.speeds:
        print("No speeds stored; run scripts/bench_backend.py, then `cairnward bench import`.")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    from cairnward.audit import AuditLog
    from cairnward.run.history import RunNotFound, find_run, list_runs, snapshots_for
    from cairnward.run.snapshots import SnapshotError, Snapshots

    cfg = _load(args)
    with AuditLog.open(cfg.paths) as audit:
        try:
            if args.runs_command == "list":
                for r in list_runs(audit)[-50:]:
                    code = "running" if r.exit_code is None else f"exit {r.exit_code}"
                    changed = "" if r.changed_files is None else f"{r.changed_files} changed"
                    print(
                        f"{r.run_id[:8]}  {r.started[:19]}  {r.client or '':10} "
                        f"{r.program or '':12} {code:8} {changed:12} {r.workspace}"
                    )
                return 0
            if args.runs_command == "prune":
                runs = list_runs(audit)
                keep = {r.run_id for r in runs[-args.keep :]} if args.keep > 0 else set()
                removed = 0
                for ws in {r.workspace for r in runs if r.workspace}:
                    removed += Snapshots(cfg.paths.state_dir, ws).prune(keep)
                print(f"Removed snapshots of {removed} run(s); kept the newest {args.keep}.")
                return 0
            run = find_run(audit, args.run)
            snaps = snapshots_for(cfg.paths.state_dir, run)
            if args.runs_command == "diff":
                print(snaps.diff(run.run_id, stat=args.stat), end="")
                return 0
            result = snaps.revert(run.run_id, force=args.force)
            audit.append(
                "run.revert",
                actor="user",
                run_id=run.run_id,
                environment=run.environment,
                client=run.client,
                details={
                    "restored": len(result.restored),
                    "conflicts": len(result.conflicts),
                    "forced": args.force,
                },
            )
            for path in result.restored:
                print(f"restored  {path}")
            for path in result.conflicts:
                print(f"skipped   {path}  (changed again since the run; --force to undo anyway)")
            print(
                f"Reverted run {run.run_id[:8]}: {len(result.restored)} file(s) restored, "
                f"{len(result.conflicts)} skipped."
            )
            return 1 if result.conflicts else 0
        except (RunNotFound, SnapshotError) as exc:
            _log(f"cairnward runs: {exc}")
            return 1


def _embedder(cfg: Config, environment: str) -> tuple[Any, Any, Any]:
    """(embedder, audit log, backends) for CLI research commands."""
    from cairnward.audit import AuditLog, enabled_for
    from cairnward.backends import Backends
    from cairnward.policy import DenyApprover, PolicyEngine, TerminalApprover
    from cairnward.research import InProcessEmbedder

    audit = AuditLog.open(cfg.paths, enabled_for(cfg))
    approver = TerminalApprover() if sys.stdin.isatty() else DenyApprover()
    backends = Backends(cfg.backends)
    engine = PolicyEngine(cfg, approver=approver, audit=audit)
    return InProcessEmbedder(cfg, engine, backends, environment=environment), audit, backends


def _library_ask(cfg: Config, research: Any, args: argparse.Namespace) -> int:
    """Through the running server when there is one (its queue keeps research from
    competing with chats for the CPU); otherwise in this process."""
    import asyncio

    import httpx

    from cairnward.research.ask import Answer, Source, run_ask
    from cairnward.run.runner import RunError, management

    research.get(args.name)  # fail early on a wrong name
    if getattr(args, "engine", "lean") == "paperqa":
        from cairnward.research.engines import paperqa

        print("Asking with PaperQA2 (slow on a CPU: several model calls per question)...",
              file=sys.stderr)  # fmt: skip
        try:
            pq = paperqa.ask(cfg, args.name, args.question, effort=args.effort,
                             evidence_k=args.evidence_k, skip_summary=args.skip_summary,
                             log=lambda m: print(m, file=sys.stderr))  # fmt: skip
        except paperqa.EngineError as exc:
            print(f"cairnward: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(pq.as_dict(), indent=2))
        else:
            sys.stdout.write(pq.render())
        return 0
    body = {"library": args.name, "question": args.question, "k": args.k, "effort": args.effort}
    data: dict[str, Any] | None = None
    try:
        with management(cfg) as mgmt:
            print("Asking through the running Cairnward (this can take a few minutes on a CPU)...",
                  file=sys.stderr)  # fmt: skip
            resp = mgmt.post("/v1/research/ask", json=body, timeout=3600)
            if resp.status_code != 200:
                print(f"cairnward: {resp.json().get('detail', resp.text)}", file=sys.stderr)
                return 1
            data = resp.json()
    except (RunError, httpx.ConnectError):
        embedder, audit, backends = _embedder(cfg, research.get(args.name).environment)
        try:
            answer = asyncio.run(run_ask(cfg, embedder.engine, backends, args.name, args.question,
                                         k=args.k, effort=args.effort, actor="user"))  # fmt: skip
        finally:
            audit.close()
            asyncio.run(backends.aclose())
        data = answer.as_dict()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    answer = Answer(**{**data, "sources": [Source(**s) for s in data["sources"]]})
    sys.stdout.write(answer.render())
    return 0


def _library_drive(cfg: Config, research: Any, args: argparse.Namespace) -> int:
    from cairnward.audit import AuditLog, enabled_for
    from cairnward.policy import PolicyEngine
    from cairnward.policy.engine import Action
    from cairnward.research import drive

    lib = research.get(args.name)
    with AuditLog.open(cfg.paths, enabled_for(cfg)) as audit:
        engine = PolicyEngine(cfg, audit=audit)
        action = Action(
            "net.connect",
            drive.HOST,
            environment=lib.environment,
            actor="user",
            details={"via": "rclone", "remote": args.remote.split(":", 1)[0]},
        )
        rule = engine.rule_for(action)
        if rule.base == "deny":
            _log(f"not allowed in environment '{lib.environment}': {rule.reason}")
            return 1
        if rule.base == "ask" and not args.yes:
            answer = input(f"Connect to Google Drive ({drive.HOST}) for '{args.name}'? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                return 1
        try:
            for warning in drive.check_remote(args.remote):
                print(f"note: {warning}")
            local = lib.folder / "drive"
            if args.preview:
                todo = drive.plan(drive.listing(args.remote), local)
            else:
                todo = drive.mirror(args.remote, local, prune=args.prune)
        except drive.DriveError as exc:
            audit.append(
                "drive.mirror",
                actor="user",
                environment=lib.environment,
                target=args.name,
                outcome="error",
                details={"error": str(exc)[:300]},
            )
            _log(str(exc))
            return 1
        counts = {k: len(v) for k, v in todo.items()}
        audit.append("drive.mirror", actor="user", environment=lib.environment, target=args.name,
                     details={**counts, "preview": args.preview, "prune": args.prune})  # fmt: skip
    verb = "would copy" if args.preview else "copied"
    print(f"Drive: {verb} {counts['new']} new and {counts['changed']} changed PDF(s); "
          f"{counts['unchanged']} unchanged; {counts['gone']} no longer on the remote"
          + (" (deleted here)" if args.prune and not args.preview else ""))  # fmt: skip
    for rel in (todo["new"] + todo["changed"])[:20]:
        print(f"  {rel}")
    if args.preview or args.no_index or not (counts["new"] or counts["changed"]):
        return 0
    research.close()
    return main(["--config-dir", str(cfg.paths.config_dir), "library", "index", args.name])


def _library_zotero(cfg: Config, research: Any, args: argparse.Namespace) -> int:
    from cairnward.audit import AuditLog, enabled_for
    from cairnward.research.zotero import Zotero, ZoteroError, import_items

    try:
        zotero = Zotero(args.url, storage=args.storage)
    except ZoteroError as exc:
        _log(str(exc))
        return 1
    try:
        if args.list_collections:
            for c in sorted(zotero.collections(), key=lambda c: c["name"].lower()):
                print(c["name"])
            return 0
        lib = research.get(args.name)
        report = import_items(zotero, lib.folder, args.collection)
    except ZoteroError as exc:
        _log(str(exc))
        return 1
    finally:
        zotero.close()
    with AuditLog.open(cfg.paths, enabled_for(cfg)) as audit:
        audit.append("zotero.import", actor="user", environment=lib.environment, target=args.name,
                     details={k: (len(v) if isinstance(v, list) else v)
                              for k, v in report.items()})  # fmt: skip
    print(f"Zotero: {report['items']} items, {report['copied']} PDFs copied, "
          f"{report['unchanged']} unchanged, {len(report['no_pdf'])} without a PDF")  # fmt: skip
    for title in report["no_pdf"][:10]:
        print(f"  no PDF: {title}")
    if args.no_index or not (report["copied"] or report["unchanged"]):
        return 0
    research.close()
    return main(["--config-dir", str(cfg.paths.config_dir), "library", "index", args.name])


def _cmd_library(args: argparse.Namespace) -> int:
    import asyncio

    from cairnward.research import EmbeddingDenied, LibraryError, Research

    cfg = _load(args)
    cmd = args.library_command
    try:
        with Research.open(cfg.paths) as research:
            if cmd == "create":
                if args.env not in cfg.environments:
                    _log(f"unknown environment '{args.env}'")
                    return 2
                lib = research.create(args.name, args.folder, args.env)
                print(
                    f"Created library '{lib.name}' for {lib.folder} "
                    f"(environment '{lib.environment}')."
                )
                print(f"Next: cairnward library index {lib.name}")
                return 0
            if cmd == "list":
                for lib, n_docs, n_chunks in research.list_libraries():
                    model = lib.embedding_model or "not indexed"
                    print(
                        f"{lib.name:20} {n_docs:5} docs {n_chunks:7} chunks  [{lib.environment}] "
                        f"{model}  {lib.folder}"
                    )
                return 0
            if cmd == "show":
                for d in research.documents(args.name):
                    title = (d.title or "")[:60]
                    print(f"{d.status:8} {d.pages or 0:4}p {d.chunks:4}c  {d.path}  {title}")
                    if d.error and d.status != "indexed":
                        print(f"{'':20}{d.error}")
                return 0
            if cmd == "remove":
                research.remove(args.name)
                print(f"Removed the index of '{args.name}' (your PDFs are untouched).")
                return 0
            if cmd == "ask":
                return _library_ask(cfg, research, args)
            if cmd == "zotero":
                return _library_zotero(cfg, research, args)
            if cmd == "drive":
                return _library_drive(cfg, research, args)
            if cmd == "set":
                from cairnward.research.parsing import set_options

                lib = research.get(args.name)
                opts = set_options(cfg.paths.data_dir, args.name, parser=args.parser,
                                   metadata=args.metadata)  # fmt: skip
                print(f"{args.name}: parser {opts['parser']}, metadata {opts['metadata']}")
                if opts["metadata"] == "grobid" and not cfg.main.research.grobid_url:
                    print('note: set [research] grobid_url = "http://127.0.0.1:8070" in '
                          "cairnward.toml (your GROBID service)")  # fmt: skip
                print(f"Re-read the PDFs with: cairnward library index {args.name} --rebuild")
                return 0
            if cmd == "refs":
                from cairnward.research.cite import from_documents

                docs = [d for d in research.documents(args.name) if d.status == "indexed"]
                from cairnward.research import grobid

                refs = from_documents(docs, research.get(args.name).folder,
                                      lambda rel: grobid.load(cfg.paths.data_dir, args.name,
                                                              rel)[0])  # fmt: skip
                print(json.dumps(refs, indent=2, ensure_ascii=False))
                return 0
            if cmd == "check":
                from cairnward.research.check import check_draft

                draft_report = check_draft(
                    research, args.name, args.draft.read_text(encoding="utf-8")
                )
                if args.json:
                    print(json.dumps(draft_report.as_dict(), indent=2))
                    return 1 if draft_report.problems else 0
                for q in draft_report.quotes:
                    mark = "ok " if q.ok else "BAD"
                    print(
                        f'[{mark}] quote "{q.quote[:70]}" '
                        + (f"-> {q.where}" if q.where else "not found in the library")
                    )
                for r in draft_report.references:
                    mark = {"in_library": "ok ", "doi_unverified": "doi", "not_found": "BAD"}[
                        r.status
                    ]
                    print(
                        f"[{mark}] reference {r.entry[:70]}" + (f" -> {r.match}" if r.match else "")
                    )
                if not draft_report.quotes and not draft_report.references:
                    print("no quotations or References section found")
                print(f"{draft_report.problems} problem(s)")
                return 1 if draft_report.problems else 0
            lib = research.get(args.name)
            embedder, audit, backends = _embedder(cfg, lib.environment)
            try:
                if cmd == "index":
                    from cairnward.research.parsing import hooks

                    parse_many, enrich = hooks(
                        cfg, args.name, lib.environment, proxy=backends.web_proxy(lib.environment)
                    )
                    report = asyncio.run(
                        research.index(
                            args.name,
                            embedder,
                            prune=args.prune,
                            rebuild=args.rebuild,
                            progress=print,
                            parse_many=parse_many,
                            enrich=enrich,
                        )
                    )
                    audit.append(
                        "library.index",
                        actor="user",
                        environment=lib.environment,
                        target=args.name,
                        details={
                            k: getattr(report, k)
                            for k in (
                                "added",
                                "updated",
                                "unchanged",
                                "no_text",
                                "failed",
                                "missing",
                                "chunks",
                                "seconds",
                            )
                        },
                    )
                    print(
                        f"\nIndexed '{args.name}': {report.added} added, {report.updated} updated, "
                        f"{report.unchanged} unchanged, {report.no_text} without text, "
                        f"{report.failed} failed, {report.missing} missing; "
                        f"{report.chunks} chunks in {report.seconds:.0f}s."
                    )
                    return 1 if report.failed else 0
                hits = asyncio.run(research.search(args.name, args.query, embedder, k=args.k))
            finally:
                audit.close()
                asyncio.run(backends.aclose())
            if args.json:
                from dataclasses import asdict

                print(json.dumps([asdict(h) for h in hits], indent=2))
                return 0
            for i, h in enumerate(hits, 1):
                title = h.title or h.path
                print(f"{i}. {title} ({h.pages})  score {h.score}")
                print(f"   {h.text[:300]}{'…' if len(h.text) > 300 else ''}\n")
            return 0
    except (LibraryError, EmbeddingDenied, ValueError) as exc:
        _log(f"cairnward library: {exc}")
        return 1


def _cmd_sandbox(args: argparse.Namespace) -> int:
    from cairnward.sandbox import SRT_VERSION, SandboxUnavailable, install, locate, node_version

    paths = default_paths(args.config_dir)
    if args.sandbox_command == "install":
        try:
            cli = install(paths)
        except (SandboxUnavailable, OSError) as exc:
            _log(f"cairnward sandbox install: {exc}")
            return 1
        print(f"Installed sandbox-runtime {SRT_VERSION}: {cli}")
        return 0
    cmd = locate(paths)
    node = node_version()
    print(
        f"sandbox-runtime: {' '.join(cmd) if cmd else 'not installed (cairnward sandbox install)'}"
    )
    print(f"node:            {'.'.join(map(str, node)) if node else 'not found'} (need 22.12+)")
    for tool in ("bwrap", "socat", "rg"):
        import shutil

        print(f"{tool + ':':16} {shutil.which(tool) or 'not found'}")
    return 0 if cmd else 1


def _privacy_dict(eff: EffectivePrivacy) -> dict[str, object]:
    return eff.as_dict()


def _print_privacy(eff: EffectivePrivacy) -> None:
    scope = f"environment '{eff.environment}'" if eff.environment else "global"
    print(f"Privacy for {scope}: {eff.label}\n")
    print(f"{'SETTING':16} {'VALUE':10} {'SOURCE':38} NOTE")
    for name in SETTINGS:
        s = eff.settings[name]
        note = "weaker than Strict" if s.weaker_than_strict else ""
        print(f"{name:16} {s.value:10} {s.source:38} {note}".rstrip())
    hosts = ", ".join(eff.network_allow_hosts) or "(none)"
    print(f"\nAllowed hosts: {hosts}")
    weaker = len(eff.weaker_than_strict)
    print(f"Weaker than Strict: {weaker} of {len(SETTINGS)} settings")
    if eff.warnings:
        print("\nWarnings:")
        for w in eff.warnings:
            print(f"  ! {w}")
    if eff.notes:
        print("\nNotes:")
        for n in eff.notes:
            print(f"  - {n}")


def _cmd_privacy_show(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if args.all:
        rows = [cfg.privacy()] + [cfg.privacy(name) for name in sorted(cfg.environments)]
        if args.json:
            print(json.dumps([_privacy_dict(e) for e in rows], indent=2))
            return 0
        print(f"{'SCOPE':20} {'PRIVACY':46} {'WEAKER THAN STRICT':19} WARNINGS")
        for eff in rows:
            name = eff.environment or "(global)"
            weaker = f"{len(eff.weaker_than_strict)}/{len(SETTINGS)}"
            print(f"{name:20} {eff.label:46} {weaker:19} {len(eff.warnings) or ''}".rstrip())
        return 0
    if args.env and args.env not in cfg.environments:
        print(
            f"Unknown environment '{args.env}'. Known: {', '.join(sorted(cfg.environments))}",
            file=sys.stderr,
        )
        return 2
    eff = cfg.privacy(args.env)
    if args.json:
        print(json.dumps(_privacy_dict(eff), indent=2))
    else:
        _print_privacy(eff)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "config":
            return {
                "paths": _cmd_config_paths,
                "check": _cmd_config_check,
                "schema": _cmd_config_schema,
            }[args.config_command](args)
        if args.command == "privacy":
            return _cmd_privacy_show(args)
        if args.command == "store":
            return _cmd_store_info(args)
        if args.command == "audit":
            return _cmd_audit(args)
        if args.command == "policy":
            return _cmd_policy_explain(args)
        if args.command == "egress":
            return _cmd_egress(args)
        if args.command == "serve":
            return _cmd_serve(args)
        if args.command == "_core":
            return _cmd_core(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "sandbox":
            return _cmd_sandbox(args)
        if args.command == "library":
            return _cmd_library(args)
        if args.command == "runs":
            return _cmd_runs(args)
        if args.command == "bench":
            return _cmd_bench(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "approvals":
            return _cmd_approvals(args)
        if args.command == "dashboard":
            return _cmd_dashboard(args)
        if args.command == "models":
            return _cmd_models(args)
        if args.command in ("research", "jobs"):
            return _cmd_jobs(args)
        if args.command == "artifacts":
            return _cmd_artifacts(args)
        if args.command == "cite":
            from cairnward.research.cite import CiteError, format_references, load_items

            paths = default_paths(args.config_dir)
            engine = args.engine
            if engine is None:
                try:
                    engine = load_config(paths).main.research.citation_engine
                except ConfigError:  # an unreadable config shouldn't stop formatting
                    engine = "auto"
            try:
                entries, used = format_references(load_items(args.file), args.style,
                                                  md=not args.plain, engine=engine,
                                                  paths=paths)  # fmt: skip
            except (CiteError, ValueError, OSError) as exc:
                print(f"cairnward cite: {exc}", file=sys.stderr)
                return 1
            print("\n\n".join(entries))
            print(f"(formatted with {used})", file=sys.stderr)
            return 0
        if args.command == "mcp":
            return _cmd_mcp(args)
        if args.command == "vault":
            from cairnward import vault_cli

            return vault_cli.run(_load(args), args, sys.stdout)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "backup":
            return _cmd_backup(args)
        if args.command == "backend":
            from cairnward.backends.providers import PROVIDERS, template

            if args.backend_command == "providers":
                for p in PROVIDERS.values():
                    mark = "may train on prompts" if p.trains_on_prompts else ""
                    print(f"{p.name:16} {p.title:40} {mark}")
                return 0
            try:
                print(template(args.provider), end="")
            except KeyError as exc:
                print(f"cairnward: {exc.args[0]}", file=sys.stderr)
                return 1
            return 0
        if args.command == "check":
            from cairnward import check

            return check.run(default_paths(args.config_dir), env=args.env,
                             paperqa=not args.no_paperqa, coding=not args.no_coding,
                             review=args.review, out=args.out)  # fmt: skip
        if args.command == "setup":
            return _cmd_setup(args)
        if args.command == "client":
            return _cmd_client(args)
        if args.command == "api":
            return _cmd_api(args)
    except ConfigError as exc:
        print("Configuration problems:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    parser.print_help()
    return 0
