"""`cairnward vault`: browse and manage Cairnward's copy of conversations (spec §4.5).

Works directly on the vault files (no running server needed). Deleting moves a
conversation to the trash (restorable for 30 days); purging destroys its key first
(crypto-shredding). Deletions, purges and exports are recorded in the audit log.
The copy in the client's own history is not affected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from cairnward.audit import AuditLog
from cairnward.config.loader import Config
from cairnward.redaction import message_texts
from cairnward.store import Vault
from cairnward.store.vault import Conversation, Message, NotFoundError, VaultError


def _row(c: Conversation, count: int | None = None) -> str:
    flags = ("P" if c.pinned else "-") + ("T" if c.deleted_at else "-")
    title = (c.title or "(untitled)")[:60]
    n = f"{count:>4}" if count is not None else ""
    return (
        f"{c.id[:10]}  {c.updated_at[:16]}  {flags}  {(c.environment or '-')[:14]:14} {n} {title}"
    )


def _text(msg: Message) -> str:
    parts = list(message_texts([msg.body]))
    return "\n".join(parts) if parts else json.dumps(msg.body, ensure_ascii=False)[:2000]


def to_markdown(conv: Conversation, msgs: list[Message]) -> str:
    out = [f"# {conv.title or 'Conversation ' + conv.id[:10]}", "",
           f"- id: {conv.id}", f"- environment: {conv.environment or '-'}",
           f"- client: {conv.client or '-'}", f"- created: {conv.created_at}", ""]  # fmt: skip
    for m in msgs:
        who = m.role + (f" ({m.model})" if m.model else "")
        out += [f"## {who}", "", _text(m), ""]
    return "\n".join(out)


def to_json(conv: Conversation, msgs: list[Message]) -> str:
    return json.dumps({
        "id": conv.id, "title": conv.title, "environment": conv.environment,
        "client": conv.client, "created_at": conv.created_at, "updated_at": conv.updated_at,
        "messages": [{"id": m.id, "parent_id": m.parent_id, "role": m.role, "model": m.model,
                      "created_at": m.created_at, "body": m.body} for m in msgs],
    }, indent=2, ensure_ascii=False)  # fmt: skip


def run(cfg: Config, args: Any, out: TextIO) -> int:
    action = args.vault_command or "list"
    with Vault.open(cfg.paths) as vault:
        try:
            return _run(cfg, vault, action, args, out)
        except (NotFoundError, VaultError) as exc:
            out.write(f"cairnward: {exc}\n")
            return 1


def _audit(cfg: Config, action: str, conv_id: str, **details: Any) -> None:
    with AuditLog.open(cfg.paths) as log:
        log.append(action, actor="user", target=conv_id, conversation_id=conv_id,
                   details=details)  # fmt: skip


def _run(cfg: Config, vault: Vault, action: str, args: Any, out: TextIO) -> int:
    if action == "list":
        env, trash = getattr(args, "env", None), getattr(args, "trash", False)
        convs = vault.list_conversations(environment=env, only_deleted=trash)
        if not convs:
            out.write("the trash is empty\n" if trash else "no conversations\n")
        for c in convs[: getattr(args, "limit", 50)]:
            out.write(_row(c, len(vault.messages(c.id))) + "\n")
        return 0
    if action == "search":
        hits = vault.search(args.text, environment=args.env)
        for c, snippet in hits:
            out.write(_row(c) + f"\n            ...{snippet}...\n")
        if not hits:
            out.write("no matches\n")
        return 0
    if action == "prune":
        result = apply_retention(cfg, vault)
        total = sum(len(v) for v in result.values())
        lines = "".join(f"\n  {k}: {len(v)}" for k, v in result.items() if v)
        out.write(f"purged {total} conversation(s){lines}\n")
        return 0

    conv_id = vault.resolve_id(args.id)
    if action == "show":
        conv, msgs = vault.conversation(conv_id), vault.messages(conv_id)
        out.write(to_json(conv, msgs) + "\n" if args.json else to_markdown(conv, msgs))
        return 0
    if action == "export":
        conv, msgs = vault.conversation(conv_id), vault.messages(conv_id)
        text = to_json(conv, msgs) if args.format == "json" else to_markdown(conv, msgs)
        if args.output:
            path = Path(args.output)
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
            out.write(f"exported to {path} (plain text: keep it safe)\n")
        else:
            out.write(text)
        _audit(cfg, "vault.export", conv_id, format=args.format, to_file=bool(args.output))
        return 0
    if action == "delete":
        vault.soft_delete(conv_id)
        _audit(cfg, "vault.delete", conv_id)
        out.write(f"moved {conv_id[:10]} to the trash (cairnward vault restore {conv_id[:10]}); "
                  "the client's own copy is not affected\n")  # fmt: skip
        return 0
    if action == "restore":
        vault.restore(conv_id)
        _audit(cfg, "vault.restore", conv_id)
        out.write(f"restored {conv_id[:10]}\n")
        return 0
    if action == "purge":
        vault.purge(conv_id, force=args.force)
        _audit(cfg, "vault.purge", conv_id, forced=args.force)
        out.write(f"purged {conv_id[:10]}: its key is destroyed; this can't be undone\n")
        return 0
    if action == "pin":
        vault.set_pinned(conv_id, not args.off)
        out.write(("unpinned " if args.off else "pinned ") + conv_id[:10] + "\n")
        return 0
    if action == "title":
        vault.set_title(conv_id, args.title)
        out.write(f"renamed {conv_id[:10]}\n")
        return 0
    raise VaultError(f"unknown action {action}")


def apply_retention(cfg: Config, vault: Vault) -> dict[str, list[str]]:
    """Per-environment retention (vault_retention_days; pinned conversations are kept)
    and emptying the trash after 30 days. Audited."""
    result: dict[str, list[str]] = {}
    for name, env in cfg.environments.items():
        if env.vault_retention_days:
            result[name] = vault.expire(name, env.vault_retention_days)
    result["trash"] = vault.purge_expired()
    if any(result.values()):
        with AuditLog.open(cfg.paths) as log:
            log.append("vault.retention", actor="cairnward",
                       details={k: len(v) for k, v in result.items()})  # fmt: skip
    return result
