"""Command-line entry point.

Subcommands so far: `config paths|check|schema` and `privacy show`. More (`serve`, `run`,
`doctor`, `audit`, ...) arrive with the Phase 0 tasks in docs/plan/phase0.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from cairnward import __version__
from cairnward.config import SETTINGS, Config, ConfigError, default_paths, load_config
from cairnward.config.paths import make_owner_only, private_by_default
from cairnward.config.privacy import EffectivePrivacy
from cairnward.config.schema import render, schemas, write_schemas


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairnward",
        description="Local privacy gateway for AI clients and agents.",
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
    prove = audit_sub.add_parser(
        "prove", help="a proof that one record is in an anchored checkpoint (Merkle, RFC 9162)"
    )
    prove.add_argument("seq", type=int, help="the record's sequence number")
    prove.add_argument("--out", type=Path, help="write the proof here (default: print it)")
    chk = audit_sub.add_parser(
        "check-proof", help="check a proof from `audit prove`, without the log"
    )
    chk.add_argument("proof", type=Path, help="the proof file")
    chk.add_argument(
        "--signers",
        type=Path,
        help="the anchors repo's signers file you trust, to check the signature",
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
    run.add_argument("--trust-program-change", action="store_true",
                     help="accept that the program changed since it last ran here")  # fmt: skip
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
    bk = sub.add_parser("backup", help="encrypted backups with restic (vault, plugins, artifacts)")
    bk_sub = bk.add_subparsers(dest="backup_command", metavar="ACTION", required=True)
    bk_use = bk_sub.add_parser("use", help="turn backups on: choose the folder and who can read it")
    bk_use.add_argument("folder", nargs="?",
                        help="full path (default: current, or ~/cairnward-backups)")  # fmt: skip
    bk_use.add_argument("--mode", choices=["auto", "on_demand"], default="auto",
                        help="auto (default): every 24 hours while the server runs")  # fmt: skip
    bk_use.add_argument("--access", choices=["owner", "group"],
                        help="who can read the folder (default: only you)")  # fmt: skip
    bk_sub.add_parser("init", help="create the repository set in [backup] repository")
    bk_sub.add_parser("run", help="back up now (and remove purged conversations from old backups)")
    bk_sub.add_parser("list", help="the snapshots")
    bk_sub.add_parser("status", help="mode, repository, last backup, pending removals")
    bk_res = bk_sub.add_parser("restore", help="bring back conversations from a backup")
    bk_res.add_argument("--snapshot", help="snapshot ID (default: the latest)")
    bk_res.add_argument("--at", help="the latest backup from this date or earlier (YYYY-MM-DD)")
    bk_res.add_argument("--to", type=Path, help="only extract the backup into this folder")
    rp = sub.add_parser("replicate", help="the same chats and artifacts on several devices")
    rp_sub = rp.add_subparsers(dest="replicate_command", metavar="ACTION", required=True)
    for name, text in (
        ("init", "start sharing through a new folder (shows the sync key once)"),
        ("join", "join a folder another device set up (asks for the sync key)"),
    ):
        rp_start = rp_sub.add_parser(name, help=text)
        rp_start.add_argument("folder", type=Path, help="Syncthing, NAS or synced-drive folder")
        rp_start.add_argument("--name", help="what other devices call this one")
        rp_start.add_argument("--mode", choices=["auto", "on_demand"], default="auto",
                              help="auto: serve syncs every few minutes")  # fmt: skip
    rp_sub.add_parser("sync", help="read the other devices and publish what's new here")
    rp_st = rp_sub.add_parser("status", help="devices, last sync, problems")
    rp_st.add_argument("--json", action="store_true", help="machine-readable output")
    rp_ok = rp_sub.add_parser("approve", help="accept a new device's changes")
    rp_ok.add_argument("device", help="its id, or the start of it")
    rp_no = rp_sub.add_parser("block", help="stop accepting a device's changes")
    rp_no.add_argument("device", help="its id, or the start of it")
    rp_sub.add_parser("leave", help="stop replicating on this device (its chats stay here)")
    chk = sub.add_parser("check", help="doctor + evals + client checks on this machine, one report")
    chk.add_argument("--no-coding", action="store_true", help="skip the coding-agent eval")
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
    plug = sub.add_parser("plugin", help="plugins: MCP servers under the plugin contract")
    plug_sub = plug.add_subparsers(dest="plugin_command", metavar="ACTION", required=True)
    plug_add = plug_sub.add_parser("add", help="add a plugin from its manifest (plugin.toml)")
    plug_add.add_argument("manifest", type=Path)
    plug_add.add_argument("--yes", action="store_true", help="don't ask")
    plug_list = plug_sub.add_parser("list", help="plugins and whether they can start")
    plug_list.add_argument("--json", action="store_true")
    plug_pin = plug_sub.add_parser("pin", help="accept a plugin's installed code as it is now")
    plug_pin.add_argument("name")
    plug_verify = plug_sub.add_parser("verify", help="run the contract's tests (needs serve)")
    plug_verify.add_argument("name")
    plug_verify.add_argument("--env", help="environment to test in")
    plug_run = plug_sub.add_parser("run", help="run a command in a plugin's sandbox")
    plug_run.add_argument("name")
    plug_run.add_argument("--env", help="environment for its key")
    plug_run.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- COMMAND")
    plug_read = plug_sub.add_parser("allow-read", help="let a plugin read one more folder")
    plug_read.add_argument("name")
    plug_read.add_argument("folder", type=Path)
    plug_help = plug_sub.add_parser(
        "helper", help="run a program a plugin needs outside its sandbox, through egress"
    )
    plug_help.add_argument("name")
    plug_help.add_argument("--env", required=True, help="environment whose network policy applies")
    plug_help.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- COMMAND")
    plug_rm = plug_sub.add_parser("remove", help="take a plugin out of mcp.toml (keeps its data)")
    plug_rm.add_argument("name")
    mcp_sub.add_parser("list", help="configured MCP servers and their permissions")
    mcp_tools = mcp_sub.add_parser("tools", help="a server's tools, pin state and warnings")
    mcp_tools.add_argument("server")
    mcp_appr = mcp_sub.add_parser("approve", help="review and pin a server's tool definitions")
    mcp_appr.add_argument("server")
    mcp_appr.add_argument("--yes", action="store_true", help="approve without asking")
    mcp_sub.add_parser("connect", help="stdio bridge to the MCP endpoint (same as no ACTION)")
    mcp_login = mcp_sub.add_parser("login", help='sign in to a remote server (auth = "oauth")')
    mcp_login.add_argument("server")
    mcp_login.add_argument(
        "--env",
        help="whose network rules the sign-in follows "
        "(default: the first environment using the server)",
    )
    mcp_login.add_argument("--no-browser", action="store_true", help="only print the address")
    mcp_logout = mcp_sub.add_parser("logout", help="forget a remote server's stored sign-in")
    mcp_logout.add_argument("server")

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
    v_rk = vlt_sub.add_parser("recovery-key", help="optional key that can reopen locked "
                              "conversations if a passphrase is forgotten")  # fmt: skip
    v_rk.add_argument("what", choices=["status", "create", "remove"], nargs="?", default="status")
    v_rec = vlt_sub.add_parser("recover", help="set a new passphrase with the recovery key")
    v_rec.add_argument("id")

    art = sub.add_parser("artifacts", help="versioned files of conversations, jobs and runs")
    art_sub = art.add_subparsers(dest="artifacts_command", metavar="ACTION")
    art_sub.add_parser("list", help="repositories (default)")
    art_actions = (
        ("files", "files in a repository"),
        ("log", "versions (of a file)"),
        ("show", "print a file"),
        ("restore", "bring back an old version"),
        ("lock", "lock a finished job's or a run's files with a passphrase"),
        ("unlock", "remove that lock and restore the files"),
        ("recover", "forgotten passphrase: set a new one with the recovery key"),
    )
    for name, text in art_actions:
        a = art_sub.add_parser(name, help=text)
        a.add_argument("scope", choices=["conversation", "job", "run", "plugin"])
        a.add_argument("owner", help="conversation, job or run id, or plugin name")
        if name in ("log",):
            a.add_argument("path", nargs="?")
        if name in ("show", "restore"):
            a.add_argument("path")
        if name == "show":
            a.add_argument("--rev")
        if name == "restore":
            a.add_argument("rev")

    red = sub.add_parser("redaction", help="the redaction scanner: optional Presidio, try it out")
    red_sub = red.add_subparsers(dest="redaction_command", metavar="ACTION", required=True)
    red_sub.add_parser("install", help="install the optional Presidio detector").add_argument(
        "engine", choices=["presidio"])  # fmt: skip
    red_sub.add_parser("status", help="is Presidio installed and answering")
    red_sub.add_parser("scan", help="show what the scanner finds in a text").add_argument("text")
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

    eca = sub.add_parser("egress-ca", help="the local CA for TLS inspection ([tls_inspection])")
    eca_sub = eca.add_subparsers(dest="ca_command", metavar="ACTION", required=True)
    eca_show = eca_sub.add_parser("show", help="where the CA is and its fingerprint")
    eca_show.add_argument("--pem", action="store_true", help="print the certificate")
    eca_sub.add_parser("rotate", help="replace the CA (restart `cairnward serve` afterwards)")

    store = sub.add_parser("store", help="local storage (vault and keys)")
    store_sub = store.add_subparsers(dest="store_command", metavar="ACTION", required=True)
    store_sub.add_parser("info", help="database files, schema versions and counts")
    store_sub.add_parser(
        "encrypt", help="encrypt vault, keys and audit databases with SQLCipher (stop serve first)"
    )
    store_sub.add_parser("decrypt", help="turn SQLCipher encryption off again (stop serve first)")
    store_sub.add_parser("passphrase", help="change the passphrase of the database key")

    priv = sub.add_parser("privacy", help="privacy settings")
    priv_sub = priv.add_subparsers(dest="privacy_command", metavar="ACTION", required=True)
    show = priv_sub.add_parser("show", help="effective privacy settings and where they come from")
    group = show.add_mutually_exclusive_group()
    group.add_argument("--env", help="show one environment (default: global settings)")
    group.add_argument("--all", action="store_true", help="one-line summary per environment")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _cmd_plugin(args: argparse.Namespace) -> int:
    from cairnward.plugins import cli as plugin_cli

    cfg = _load(args)
    action = args.plugin_command
    if action == "add":
        return plugin_cli.add(cfg, args.manifest, yes=args.yes)
    if action == "list":
        return plugin_cli.list_plugins(cfg, args.json)
    if action == "pin":
        return plugin_cli.pin(cfg, args.name)
    if action == "verify":
        return plugin_cli.verify(cfg, args.name, args.env)
    if action == "remove":
        return plugin_cli.remove(cfg, args.name)
    if action == "allow-read":
        return plugin_cli.allow_read(cfg, args.name, args.folder)
    if action == "helper":
        return plugin_cli.helper(cfg, args.name, args.env, [c for c in args.cmd if c != "--"])
    return plugin_cli.run(cfg, args.name, args.env, [c for c in args.cmd if c != "--"])


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
    for name, backend in cfg.backends.backends.items():
        if warning := backend.unencrypted_warning(name):
            print(f"  warning: {warning}")
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
    from cairnward.store import dbkey

    state = "SQLCipher" + (" (key by reference)" if dbkey.configured_reference(paths) else "")
    print(f"{'encryption':14} {state if dbkey.enabled(paths) else 'off (plain SQLite)'}")
    return 0


def _new_passphrase(prompt: str) -> str:
    import getpass

    first = getpass.getpass(f"{prompt}: ")
    if len(first) < 8:
        raise SystemExit("cairnward: use at least 8 characters")
    if getpass.getpass("Again: ") != first:
        raise SystemExit("cairnward: the two passphrases differ")
    return first


def _cmd_store_crypt(args: argparse.Namespace) -> int:
    """`store encrypt | decrypt | passphrase` (ADR-0019)."""
    import getpass

    from cairnward.audit import AuditLog
    from cairnward.core.server import _already_running
    from cairnward.store import Vault, dbkey
    from cairnward.store.db import DatabaseKeyError, convert, is_encrypted

    cfg = _load(args)
    paths = cfg.paths
    if args.store_command != "passphrase" and _already_running(cfg):
        print("cairnward: stop `cairnward serve` first: the databases are rewritten",
              file=sys.stderr)  # fmt: skip
        return 1
    files = [paths.data_dir / name for name in dbkey.DATABASES]
    try:
        if args.store_command == "passphrase":
            if not dbkey.enabled(paths):
                print("cairnward: the databases aren't encrypted", file=sys.stderr)
                return 1
            old = getpass.getpass("Current passphrase: ")
            dbkey.rewrap(paths, old, _new_passphrase("New passphrase"))
            with AuditLog.open(paths) as log:
                log.append("store.passphrase", actor="user", outcome="ok")
            print("Passphrase changed. The databases themselves are unchanged.")
            return 0
        if args.store_command == "encrypt":
            if dbkey.enabled(paths):
                print("cairnward: the databases are already encrypted", file=sys.stderr)
                return 1
            if not dbkey.pending_file(paths).exists():
                Vault.open(paths).close()  # create and migrate them first, plain
                AuditLog.open(paths).close()
                print("The passphrase is asked whenever Cairnward starts. Without it (or the "
                      "key) the vault, its keys and the audit log can't be opened "
                      "again.")  # fmt: skip
                passphrase = _new_passphrase("New passphrase for the databases")
            else:
                print("Finishing an encryption that was interrupted.")
                passphrase = getpass.getpass("Its passphrase: ")
            key = dbkey.begin(paths, passphrase)
            for path in files:
                if path.exists() and not is_encrypted(path):
                    convert(path, key, encrypt=True)
            dbkey.commit(paths, key)
            mirror = paths.data_dir / "audit.jsonl"
            mirror.unlink(missing_ok=True)  # a plaintext copy of the audit log
            with AuditLog.open(paths) as log:
                names = [p.name for p in files if p.exists()]
                log.append("store.encrypted", actor="user", outcome="ok",
                           details={"databases": names})  # fmt: skip
            print("Encrypted: " + ", ".join(p.name for p in files if p.exists()) + ".")
            print("audit.jsonl (the plaintext mirror) was removed; `audit log --json` exports.")
            print("Old plaintext may remain in the disk's free space: full-disk encryption is "
                  "still advised.")  # fmt: skip
            return 0
        # decrypt
        if not dbkey.enabled(paths):
            print("cairnward: the databases aren't encrypted", file=sys.stderr)
            return 1
        db_key = dbkey.key_for(paths)
        if db_key is None:  # pragma: no cover - enabled() was checked
            return 1
        with AuditLog.open(paths) as log:
            log.append("store.decrypted", actor="user", outcome="ok")
        for path in files:
            if path.exists() and is_encrypted(path):
                convert(path, db_key, encrypt=False)
        dbkey.key_file(paths).unlink()
        dbkey.forget(paths)
        AuditLog.open(paths).close()  # writes the plaintext mirror again
        print("Decrypted. The databases are plain SQLite again.")
        return 0
    except DatabaseKeyError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1


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

    if args.audit_command == "check-proof":  # works anywhere: needs neither log nor config
        return _audit_check_proof(args)
    paths = default_paths(args.config_dir)
    with AuditLog.open(paths) as log:
        if args.audit_command == "head":
            seq, h = log.head()
            print(f"{seq}:{h}")
            return 0
        if args.audit_command == "anchor":
            return _audit_anchor(args, log)
        if args.audit_command == "prove":
            return _audit_prove(args, log)
        if args.audit_command == "verify":
            res = log.verify(_parse_expect(args.expect))
            for note in res.notes:
                print(f"note: {note}")
            if res.ok:
                what = "chain intact" if log.mirror is None else "chain and mirror intact"
                print(f"OK: {res.records} records, {what}.")
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


def _audit_prove(args: argparse.Namespace, log: Any) -> int:
    import json

    from cairnward.audit.anchor import Anchorer, AnchorError, make_proof

    cfg = _load(args)
    try:
        bundle = make_proof(Anchorer(cfg.paths, cfg.main.anchor), log, args.seq)
    except AnchorError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(bundle, indent=1, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"proof for record {args.seq} in the checkpoint at seq {bundle['size']}: {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def _audit_check_proof(args: argparse.Namespace) -> int:
    import json

    from cairnward.audit.anchor import check_proof

    try:
        bundle = json.loads(args.proof.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cairnward: can't read {args.proof}: {exc}", file=sys.stderr)
        return 1
    problems = check_proof(bundle if isinstance(bundle, dict) else {}, args.signers)
    unsigned = [p for p in problems if p.startswith("signature not checked")]
    if problems and problems != unsigned:
        print("proof FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    rec = bundle["record"]
    print(f"OK: record {rec['seq']} ({rec['action']}, {rec['ts']}) is in the checkpoint at seq "
          f"{bundle['size']}, signed {bundle['checkpoint']['payload'].get('ts')}")  # fmt: skip
    if unsigned:
        print(f"note: {unsigned[0]}")
        return 2
    print("signature OK")
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
        if res.timestamped:
            print(f"timestamps OK: {res.timestamped} checkpoint(s) timestamped by the authority, "
                  f"the last at {res.last_tsa_time}")  # fmt: skip
        if res.merkle_roots:
            print(f"Merkle roots OK: {res.merkle_roots} checkpoint(s) match the log, each "
                  "extending the one before")  # fmt: skip
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


def _cmd_egress_ca(args: argparse.Namespace) -> int:
    from cairnward.audit import AuditLog, enabled_for
    from cairnward.egress import tls

    cfg = _load(args)
    using = [n for n, e in cfg.environments.items() if e.tls_inspection.enabled]
    if args.ca_command == "rotate":
        old = tls.ca_dir(cfg.paths.data_dir) / tls.CA_CERT
        before = tls.ca_fingerprint(old.read_bytes()) if old.is_file() else None
        tls.rotate_ca(cfg.paths.data_dir)
        after = tls.ca_fingerprint(tls.ca_cert_pem(cfg.paths.data_dir))
        with AuditLog.open(cfg.paths, enabled_for(cfg)) as log:
            log.append("egress.ca_rotate", actor="user", target="egress-ca",
                       details={"old_fingerprint": before, "new_fingerprint": after})  # fmt: skip
        print(f"New CA: {after}")
        print("Restart `cairnward serve`; runs started after that trust the new CA.")
        return 0
    if not using and not (tls.ca_dir(cfg.paths.data_dir) / tls.CA_CERT).is_file():
        print("TLS inspection is off in every environment, and no CA has been made.")
        return 0
    pem = tls.ca_cert_pem(cfg.paths.data_dir)
    if args.pem:
        sys.stdout.write(pem.decode())
        return 0
    print(f"CA:          {tls.ca_dir(cfg.paths.data_dir) / tls.CA_CERT}")
    print(f"fingerprint: {tls.ca_fingerprint(pem)}")
    print(f"inspecting:  {', '.join(using) if using else 'no environment'}")
    return 0


def _cmd_egress(args: argparse.Namespace) -> int:
    import asyncio

    from cairnward.audit import AuditLog, enabled_for, record_settings
    from cairnward.egress import Session, Sessions
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
        from cairnward.core.server import sockets
        from cairnward.egress.rust import make_egress

        proxy = make_egress(cfg, engine, sessions, port=args.port,
                            socket_dir=sockets(cfg)["egress"].parent)  # fmt: skip

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
    if changed := make_owner_only(cfg.paths):
        _log(f"Made {changed} file(s) in Cairnward's folders owner-only.")
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
            trust_program_change=args.trust_program_change,
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


def _mcp_sign_in(cfg: Config, args: argparse.Namespace, action: str) -> int:
    import asyncio
    import webbrowser

    from cairnward.mcp import oauth
    from cairnward.mcp.hub import resolve_headers
    from cairnward.run.runner import RunError, egress_session

    spec = cfg.mcp.servers.get(args.server)
    if spec is None or spec.auth != "oauth" or not spec.url:
        print(f'cairnward: {args.server} isn\'t a remote server with auth = "oauth" in mcp.toml',
              file=sys.stderr)  # fmt: skip
        return 1
    storage = oauth.FileTokenStorage(oauth.token_file(cfg.paths, args.server))
    if action == "logout":
        print("forgotten" if storage.forget() else "not signed in")
        return 0
    env = args.env or next((n for n, e in cfg.environments.items()
                            if args.server in e.mcp_servers), None)  # fmt: skip
    if env is None:
        print(f"cairnward: no environment uses {args.server} (mcp_servers)", file=sys.stderr)
        return 1

    def show(url: str) -> None:
        print(f"Sign in here (waiting for up to 5 minutes):\n{url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

    try:
        with egress_session(cfg, env, "mcp-login") as proxy:
            tools = asyncio.run(oauth.login(cfg.paths, args.server, spec.url, spec.oauth_scope,
                                            headers=resolve_headers(spec.headers), proxy=proxy,
                                            open_url=show))  # fmt: skip
    except (RunError, oauth.LoginNeeded) as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # the SDK raises many kinds, in exception groups too
        storage.forget()
        print(f"cairnward: sign-in failed: {exc}", file=sys.stderr)
        return 1
    print(f"signed in to {args.server}: {len(tools)} tool(s). Review them with "
          f"`cairnward mcp approve {args.server}`.")  # fmt: skip
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
    if action in ("login", "logout"):
        return _mcp_sign_in(cfg, args, action)

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
                if s.get("url"):
                    print(f"  remote  : {s['url']} (through egress; the environment's network "
                          "rules apply)")  # fmt: skip
                    if s.get("headers"):
                        print(f"  headers : {', '.join(s['headers'])}")
                    if s.get("auth") == "oauth":
                        from cairnward.mcp.oauth import FileTokenStorage, token_file

                        state = FileTokenStorage(token_file(cfg.paths, s["name"])).status()
                        needed = f"needed: cairnward mcp login {s['name']}"
                        print(f"  sign-in : {'signed in' if state['signed_in'] else needed}")
                    print(f"  enabled : {', '.join(s['environments']) or 'nowhere (mcp_servers)'}")
                    continue
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


def _cmd_redaction(args: argparse.Namespace) -> int:
    import subprocess

    from cairnward.redaction import Scanner, scan_texts
    from cairnward.redaction_engines import presidio

    cfg = _load(args)
    if args.redaction_command == "install":
        try:
            record = presidio.install(cfg.paths)
        except (presidio.PresidioError, subprocess.CalledProcessError) as exc:
            print(f"cairnward: {exc}", file=sys.stderr)
            return 1
        print(f"Presidio installed (lock sha256 {record['lock_sha256'][:16]}...). Switch it on "
              "with [redaction] presidio = true in cairnward.toml.")  # fmt: skip
        return 0
    if args.redaction_command == "status":
        if presidio.python(cfg.paths) is None:
            print("presidio: not installed")
            return 1
        try:
            presidio.shared(cfg.paths).analyze("Ada Lovelace lived in London.", ["PERSON"], 0.0)
        except presidio.PresidioError as exc:
            print(f"presidio: installed but not working: {exc}")
            return 1
        on = "on" if cfg.main.redaction.presidio else "off ([redaction] presidio = true)"
        print(f"presidio: installed and answering; {on}")
        return 0
    scanner = Scanner.from_config(cfg.main.redaction, cfg.paths)
    found = scanner.scan(args.text)
    for f in found:
        print(f"{f.category:9} {f.kind:22} {args.text[f.start : f.end]!r}")
    if not found:
        print("nothing found")
    for note in scan_texts(scanner, []).unavailable or scanner.unavailable:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    from cairnward import backup

    cfg = _load(args)
    action = args.backup_command
    try:
        with backup.remote_access(cfg):
            return _backup_action(cfg, args, action)
    except backup.BackupError as exc:
        print(f"cairnward backup: {exc}", file=sys.stderr)
        return 1


def _cmd_replicate(args: argparse.Namespace) -> int:
    from cairnward import replication
    from cairnward.config.edit import EditError

    try:
        return _replicate_action(_load(args), args, args.replicate_command)
    except (replication.ReplicationError, EditError) as exc:
        print(f"cairnward replicate: {exc}", file=sys.stderr)
        return 1


def _replicate_action(cfg: Config, args: argparse.Namespace, action: str) -> int:
    import getpass

    from cairnward import replication

    if action == "init":
        folder = args.folder.expanduser().resolve()
        key = replication.init(cfg, folder, name=args.name, mode=args.mode)
        print(f"Sharing through {folder}. Every conversation here will be published to it at the "
              "next sync.\nThe sync key, for joining from your other devices. It is shown once "
              "and opens everything in the folder, so keep it like a password:")  # fmt: skip
        print(f"  {key}")
        warning = replication.cloud_warning(folder)
        if warning:
            print(f"Note: {warning}.")
        return 0
    if action == "join":
        folder = args.folder.expanduser().resolve()
        key = getpass.getpass("Sync key (not shown): ")
        me = replication.join(cfg, folder, key, name=args.name, mode=args.mode)
        print(f"Joined {folder} as {me.device[:10]}. On a device already there, run:\n"
              f"  cairnward replicate approve {me.device[:10]}\n"
              "Until then it can read the folder, but its changes aren't accepted.")  # fmt: skip
        warning = replication.cloud_warning(folder)
        if warning:
            print(f"Note: {warning}.")
        return 0
    if action == "sync":
        r = replication.sync(cfg, print)
        print(f"sent {r['sent']}, received {r['received']} messages, {r['purged']} purged, "
              f"{r['seconds']:.1f}s")  # fmt: skip
        for d in r["pending_devices"]:
            print(f"device {d[:10]} is waiting: cairnward replicate approve {d[:10]}")
        for item in r["waiting"]:
            print(f"waiting for more files from the folder: {item}")
        for problem in r["problems"]:
            print(f"problem: {problem}", file=sys.stderr)
        return 1 if r["problems"] else 0
    if action == "status":
        st = replication.status(cfg)
        if args.json:
            print(json.dumps(st, indent=2))
            return 0
        if not st["configured"]:
            print("not replicating (cairnward replicate init <folder>, or join)")
            return 0
        print(f"mode: {st['mode']}\nfolder: {st['folder']}\nthis device: {st['name']} "
              f"({st['device'][:10]})")  # fmt: skip
        for d in st["devices"]:
            mark = " (this device)" if d["this"] else ""
            print(f"  {d['id'][:10]}  {d['status']:9} {d['name']}{mark}")
        last = st["last"]
        print(f"last sync: {last['time'] if last else 'never'}")
        for problem in [st["problem"], st["cloud_warning"], *(last["problems"] if last else [])]:
            if problem:
                print(f"note: {problem}")
        return 0
    if action in ("approve", "block"):
        status: Literal["approved", "blocked"] = "approved" if action == "approve" else "blocked"
        device = replication.set_device(cfg, args.device, status)
        print(f"{device[:10]} is {status}.")
        if action == "block":
            print("It can still read the folder with the sync key it holds. To shut it out, "
                  "start a new folder: replicate leave, then init on each device.")  # fmt: skip
        return 0
    replication.leave(cfg)
    print("This device no longer replicates. Its chats stay here.")
    return 0


def _backup_action(cfg: Config, args: argparse.Namespace, action: str) -> int:
    import tempfile

    from cairnward import backup

    if action == "use":
        from cairnward.config.edit import EditError, change_backup

        wanted = args.folder and str(Path(args.folder).expanduser().absolute())
        try:
            cfg, changed = change_backup(cfg, args.mode, wanted or cfg.main.backup.repository,
                                         access=args.access)  # fmt: skip
        except EditError as exc:
            print(f"cairnward: {exc}", file=sys.stderr)
            return 1
        backup._audit(cfg, "backup.settings", changed=changed, mode=args.mode)
        who = "you and your group" if cfg.main.backup.access == "group" else "only you"
        print(f"Backups: {args.mode}, to {backup.repository(cfg)}, readable by {who}.")
        if warning := backup.same_disk_warning(cfg):
            print(f"note: {warning}.")
        if not backup.restic():
            print("restic isn't installed (e.g. sudo apt install restic). Then: cairnward backup "
                  "init")  # fmt: skip
            return 0
        action = "init"
        if changed:
            print("Restart cairnward serve for automatic backups to start.")
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
        print("(each holds the conversations, plugins' data and artifacts as they were)")
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
        print(f"last backup: {last['time'] + ' (' + last['snapshot'] + ')' if last else 'never'}")
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
            files = backup.import_files(cfg, folder)
        print(f"from backup {snap['short_id']} ({snap['time'][:19]}): "
              f"{counts['added']} conversation(s) brought back, {counts['skipped']} already "
              "here (left as they are)")  # fmt: skip
        print(f"artifacts, jobs and plugins' data: {files['added']} brought back, "
              f"{files['skipped']} already here (left as they are)")  # fmt: skip
        return 0
    return 2


def _cmd_artifacts(args: argparse.Namespace) -> int:
    from cairnward.artifacts import LOCKED_FILES, ArtifactError, ArtifactStore
    from cairnward.store import Vault

    paths = _load(args).paths
    action = args.artifacts_command or "list"
    with Vault.open(paths) as vault:  # unlocking is per process: locked stays locked here
        store = ArtifactStore(paths.data_dir, keys=vault)
        try:
            if action in ("lock", "unlock", "recover"):
                return _artifacts_lock(args, vault, action)
            if action == "list":
                repos = store.repos()
                if not repos:
                    print("no artifacts yet")
                for scope, owner, n in repos:
                    print(f"{scope:12} {owner}  "
                          + ("locked" if n == LOCKED_FILES else f"{n} file(s)"))  # fmt: skip
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


def _artifacts_lock(args: argparse.Namespace, vault: Any, action: str) -> int:
    from cairnward.artifacts import lock_item, unlock_item
    from cairnward.audit import AuditLog
    from cairnward.store.crypto import DecryptionError
    from cairnward.store.vault import LockedError, VaultError
    from cairnward.vault_cli import ask_passphrase

    cfg = _load(args)
    data, item = cfg.paths.data_dir, f"{args.scope}:{args.owner}"
    try:
        if action == "unlock":
            unlock_item(vault, data, args.scope, args.owner, ask_passphrase("Passphrase: "))
            print(f"removed the lock from {args.scope} {args.owner}")
        else:
            identity = ask_passphrase("Recovery key (AGE-SECRET-KEY-1...): ") if (
                action == "recover") else None  # fmt: skip
            first = ask_passphrase("New passphrase: ")
            if ask_passphrase("Again: ") != first:
                print("the passphrases differ: nothing changed", file=sys.stderr)
                return 1
            if identity is not None:
                vault.recover(item, identity, first)
                print(f"{args.scope} {args.owner} has a new passphrase (still locked)")
            else:
                lock_item(vault, data, args.scope, args.owner, first)
                print(f"locked {args.scope} {args.owner}: only the passphrase opens it, and "
                      "`cairnward artifacts unlock` restores it")  # fmt: skip
    except (VaultError, LockedError, DecryptionError) as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    with AuditLog.open(cfg.paths) as log:
        log.append(f"artifacts.{action}", actor="user", target=item)
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
    from cairnward.sandbox import openshell

    oshell = openshell.locate()
    print(f"{'openshell:':16} {' '.join(oshell) if oshell else 'not found'} "
          '(only for sandbox = "openshell")')  # fmt: skip
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


MOVED = {"library", "research", "jobs", "cite"}  # ADR-0017


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    if given and given[0] in MOVED:
        print(f"`cairnward {given[0]}` moved to the research plugin: cairnward-research "
              f"{given[0]} ... (see docs/plugins.md)", file=sys.stderr)  # fmt: skip
        return 2
    private_by_default()
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
            if args.store_command in ("encrypt", "decrypt", "passphrase"):
                return _cmd_store_crypt(args)
            return _cmd_store_info(args)
        if args.command == "audit":
            return _cmd_audit(args)
        if args.command == "policy":
            return _cmd_policy_explain(args)
        if args.command == "egress":
            return _cmd_egress(args)
        if args.command == "egress-ca":
            return _cmd_egress_ca(args)
        if args.command == "serve":
            return _cmd_serve(args)
        if args.command == "_core":
            return _cmd_core(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "sandbox":
            return _cmd_sandbox(args)
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
        if args.command == "artifacts":
            return _cmd_artifacts(args)
        if args.command == "mcp":
            return _cmd_mcp(args)
        if args.command == "plugin":
            return _cmd_plugin(args)
        if args.command == "vault":
            from cairnward import vault_cli

            return vault_cli.run(_load(args), args, sys.stdout)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "backup":
            return _cmd_backup(args)
        if args.command == "replicate":
            return _cmd_replicate(args)
        if args.command == "redaction":
            return _cmd_redaction(args)
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

            return check.run(default_paths(args.config_dir), coding=not args.no_coding,
                             out=args.out)  # fmt: skip
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
