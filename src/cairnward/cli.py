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
    log_cmd = audit_sub.add_parser("log", help="show records")
    log_cmd.add_argument("--last", type=int, default=20, help="number of records (default 20)")
    log_cmd.add_argument("--action", help="only this action, e.g. settings.changed")
    log_cmd.add_argument("--conversation", help="only records for this conversation")
    log_cmd.add_argument("--json", action="store_true", help="one JSON record per line")

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
        if args.audit_command == "verify":
            res = log.verify(_parse_expect(args.expect))
            for note in res.notes:
                print(f"note: {note}")
            if res.ok:
                print(f"OK: {res.records} records, chain and mirror intact.")
                print(f"Head {res.head_seq}:{res.head_hash}")
                return 0
            print(
                f"FAILED: {len(res.problems)} problem(s) in {res.records} records", file=sys.stderr
            )
            for p in res.problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        records = log.records(action=args.action, conversation_id=args.conversation, last=args.last)
        for r in records:
            if args.json:
                print(r.to_json_line())
                continue
            bits = [r.phase, r.decision or "", r.outcome or "", r.target or ""]
            where = " ".join(b for b in bits if b)
            scope = f" [{r.environment}]" if r.environment else ""
            print(f"{r.seq:>6} {r.ts} {r.actor:16} {r.action:20} {where}{scope}")
    return 0


def _cmd_policy_explain(args: argparse.Namespace) -> int:
    from typing import get_args

    from cairnward.policy import Action, PolicyEngine
    from cairnward.policy.engine import Kind

    if args.kind not in get_args(Kind):
        print(f"Unknown action kind '{args.kind}'. Known: {', '.join(get_args(Kind))}",
              file=sys.stderr)  # fmt: skip
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
            print(f"cairnward-egress listening on 127.0.0.1:{port} "
                  f"(environment '{env}', {cfg.privacy(env).label})", file=sys.stderr)  # fmt: skip
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


def _cmd_serve(args: argparse.Namespace) -> int:
    import asyncio

    import uvicorn

    from cairnward.audit import AuditLog, enabled_for, record_settings
    from cairnward.backends import Backends
    from cairnward.egress import EgressProxy, Session, Sessions
    from cairnward.gateway import ClientKeys, GatewayDeps, create_app
    from cairnward.policy import DenyApprover, PolicyEngine, TerminalApprover
    from cairnward.store import Vault

    cfg = _load(args)
    server = cfg.main.server
    approver = TerminalApprover() if sys.stdin.isatty() else DenyApprover()
    with AuditLog.open(cfg.paths, enabled_for(cfg)) as log, Vault.open(cfg.paths) as vault:
        sync = record_settings(cfg, log)
        engine = PolicyEngine(cfg, approver=approver, audit=log)
        sessions = Sessions()
        tokens = {env: sessions.create(Session(environment=env, actor="cairnward"))
                  for env in cfg.environments}  # fmt: skip

        def egress_url(env: str | None) -> str | None:
            if args.no_egress:
                return None
            token = tokens.get(env or cfg.main.defaults.environment)
            return f"http://cw:{token}@127.0.0.1:{server.egress_port}"

        backends = Backends(cfg.backends, egress_url_for=egress_url)
        app = create_app(
            GatewayDeps(cfg, engine, backends, ClientKeys.at(cfg.paths.state_dir), vault)
        )
        uv = uvicorn.Server(uvicorn.Config(app, host=server.gateway_host, port=server.gateway_port,
                                           log_level="warning", access_log=False))  # fmt: skip

        async def run() -> None:
            tasks = [uv.serve()]
            if not args.no_egress:
                proxy = EgressProxy(cfg, engine, sessions)
                await proxy.start()
                tasks.append(proxy.serve_forever())
                print(f"cairnward-egress on 127.0.0.1:{proxy.port}", file=sys.stderr)
            print(f"Cairnward gateway on http://{server.gateway_host}:{server.gateway_port}/v1 "
                  f"(privacy: {cfg.privacy().label})", file=sys.stderr)  # fmt: skip
            if sync.changes:
                print(f"Recorded {len(sync.changes)} settings change(s) in the audit log.",
                      file=sys.stderr)  # fmt: skip
            if not sys.stdin.isatty():
                print("No terminal: requests that need approval are denied.", file=sys.stderr)
            try:
                await asyncio.gather(*tasks)
            finally:
                await backends.aclose()

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("\nstopped", file=sys.stderr)
    return 0


def _privacy_dict(eff: EffectivePrivacy) -> dict[str, object]:
    return {
        "environment": eff.environment,
        "label": eff.label,
        "base_preset": eff.base_preset,
        "custom": eff.is_custom,
        "weaker_than_strict": eff.weaker_than_strict,
        "settings": {
            n: {"value": s.value, "source": s.source, "weaker_than_strict": s.weaker_than_strict}
            for n, s in eff.settings.items()
        },
        "changed_from_preset": eff.changed_from_preset,
        "network_allow_hosts": eff.network_allow_hosts,
        "warnings": eff.warnings,
        "notes": eff.notes,
    }


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
