# Limitations of Cairnward's security, privacy and auditing

Written 2026-11-06. This page collects in one place what Cairnward's protections don't do. The
detail is spread across the [threat model](threat-model.md) (residuals in each row, and §6
accepted risks), the [status page](status.md) (known issues, I-numbers) and the
[spec](spec.md). Those stay the sources of record; this page points to them. Where they
disagree with the code, the code was checked, and this page follows it.

## Scope and maturity
- **One person, one machine.** Cairnward is built for a single user on their own computer.
  - Multi-user or team servers, remote access and cloud hosting are non-goals (spec §1.2).
  - There are no user accounts, roles or single sign-on.
  - It listens only on this machine, and won't bind elsewhere.
  - Whoever controls the OS account controls Cairnward.
- **Not production ready.** The same assistant that wrote the code also reviewed it and wrote
  the threat model. No independent security review has been done (A10).
  - Several features have only been tested against stand-ins: hosted model providers, remote MCP
    servers and OAuth, chat clients, OpenShell, TLS inspection with real agents, and restic to a
    real remote.
  - It has never run on real WSL2, so Windows support is unproven (A1, I19).
  - A real backup has never been restored (A5).
  - The release and signing workflow has never run on GitHub (A8).
  - The egress proxy and audit log, the most security-critical parts, are Python with a large
    dependency tree (hash-locked). A Rust rewrite is deferred with no date (ADR-0003, spec §9).
  - The threat model maps to the OWASP Top 10 for LLM Applications, not yet to the Top 10 for
    Agentic Applications ([landscape.md](landscape.md)).
  - See the status page's testing lists for the full picture.
- **Isolation is Linux-only.** The OS sandbox (srt, bubblewrap) runs on Linux, and on Windows only
  through WSL2. On other systems, or with `isolation = "off"`, every network and file rule is
  advisory: a tool can connect or read directly (spec §5.0).

## Security

**What it trusts**
- **A compromised OS account or root** can read everything that's unlocked. It can also change
  the configuration, and capture passphrases as they're typed. Under WSL2 that includes any
  Windows program running as you (T-28, I2).
- **The sandbox itself.** Cairnward relies on srt, bubblewrap and seccomp, or on OpenShell when
  that's chosen. A bug in them weakens every guarantee. `cairnward doctor` self-tests the sandbox,
  but it can't prove the sandbox correct.
- **In the core's sandbox,** srt's own seccomp helper is off, because the core needs Unix
  sockets. The core's own filter refuses VM sockets and io_uring (I1). srt's refusal to create
  namespaces stays off there; srt documents that dropping all capabilities is then what keeps
  hidden paths hidden (T-28).
- **OpenShell** (`sandbox = "openshell"`) is trusted to enforce its sandbox, and has never been
  run with Cairnward for real. It pulls images itself, outside egress and the audit. Its gateway
  sends telemetry unless you turn that off (T-36).

**Network**
- **Rules are per host, not per purpose.** Once a host is allowed, an agent can send it anything
  it can read, and the request looks like any other. Exfiltrating to an allowed host can't be told
  apart from legitimate use (T-17).
- **Without TLS inspection,** Cairnward sees only the host, the port, byte counts and timing of
  third-party HTTPS. Domain fronting through a shared CDN can't be detected (T-34).
- **TLS inspection** (off by default) has its own limits:
  - It covers `cairnward run` agents only.
  - It checks method and path, but passes request bodies on without redacting them.
  - It offers only HTTP/1.1, and doesn't read WebSocket messages after the upgrade.
  - It breaks tools that pin certificates or ignore `SSL_CERT_FILE` (I27, I30).
  - Its CA key could impersonate any site to the tools that trust it (T-33). Sandboxed runs
    can't read it, but with isolation off an agent running as you can (I37).
- **Plain HTTP is allowed in two places,** with a warning or an audit record but no requirement
  to encrypt: to backends on your local network (I3), and from tools to allowed hosts (I4).
- **The gateway doesn't check `Origin`,** so desktop apps that send one keep working. A web page
  is still refused by the `Host` check, the API keys and the absence of CORS headers (spec §4.1,
  T-01, I38). The dashboard does check `Origin`.
- **Programs that aren't sandboxed** can reach local model servers, such as Ollama on
  127.0.0.1:11434, directly, bypassing Cairnward (spec §3.2). That includes chat clients not run
  in the provided containers.

**Agents, tools and plugins**
- **Inside the workspace, an agent can do anything:** read, change, delete, or run any command
  in the sandbox. Snapshots let you review and undo changes; they don't prevent them.
- **What a run can still read** with the home folder hidden: `~/.gitconfig`, where a token in a
  URL is readable, the agent's own state folder, and for most profiles `~/.cache` (T-12).
  - With `home_access = "readable"`, the agent reads the whole home folder except the known
    secret folders.
  - The list of known secret folders can't name everything.
- **Prompt injection** isn't prevented, only limited: approvals, network off by default, and
  user-only actions confirmed in Cairnward's own interface. A person can still approve without
  reading (T-14).
- **MCP tool checks are heuristic.** Pinning catches a changed definition. Scanning descriptions
  for instruction-like text catches common patterns, not every one (T-04).
  - An approved tool with write access can still be steered by injected content.
  - Remote MCP servers and OAuth have only been tested against local stand-ins (T-22).
- **Plugins:** redaction only catches what it recognises, so a plugin's legitimate requests to a
  listed host can still carry private text (T-29).
- **With isolation off,** an agent runs as you, and could answer its own approvals through the
  management socket.

## Privacy

**The vault**
- **Unlocked conversations aren't protected from someone with the disk.** Each conversation's
  key sits unwrapped in `keys.db`, on the same disk as the vault. Per-conversation encryption is
  there to make deletion reliable (crypto-shredding). Only two things keep the data from someone
  who has the disk: locking a conversation with a passphrase, and full-disk encryption (A7).
- **With `crypto_shred = "off"`** (the Open preset), conversation bodies and titles are stored as
  plain text.
- **Metadata stays readable** even when content is encrypted: when a conversation was created
  and updated, its environment and client, its folder, and whether it's pinned.
- **Locking doesn't erase earlier copies:** plaintext written before locking, on disk or in old
  backups, can remain. Run snapshots can't be locked at all (I7).
- **Deleting on an SSD is best effort.** Old backups keep deleted data unless
  `backup_purge` rewrites them (T-10).
- **A recovery key** opens every locked conversation it covers. Keeping it safe is up to you.
- **Clients keep their own copies** of conversations, outside Cairnward's control: no locking,
  no purge (T-18).

**What leaves the machine**
- **Redaction is incomplete.** It uses patterns, plus Presidio's small English model if
  installed, which misses some names (I8).
  - It reads only text, and tool-call arguments. Images, audio and other attachments are sent
    unscanned.
  - It covers traffic Cairnward handles: model, MCP and plugin requests. Tools' own HTTPS
    bodies aren't redacted, even with TLS inspection.
  - Organisation-specific identifiers (client names, project codes) are caught only by custom
    patterns you write.
- **Hosted models see everything they're sent,** in plain text. Cairnward controls whether and
  when they're used.
  - `trains_on_prompts` depends on you reading the provider's terms.
  - No hosted provider has been tried live.
- **Reasoning written into an answer:** a model already recorded as answering cleanly that
  starts leaking reasoning reaches the client once before it's caught (T-26).
- **Sync timing and size** can reveal activity to a cloud provider (P-04).

## Auditing

**What the log records**
- **Records are content-free.** The log holds digests, so on its own it can't show what was said
  or sent; that needs the vault, which may have been purged.
- **A digest of something short and predictable,** such as a URL path or a common prompt, can be
  reversed by guessing (T-35).
- **Activity outside Cairnward isn't recorded:** unsandboxed programs, direct connections with
  isolation off, and what an agent does inside its workspace beyond the run's start, end and
  changed files.

**What protects it**
- **It's tamper-evident, not tamper-proof.** The hash chain catches edits, deletions and
  reordering. Someone with write access can still rewrite every record from some point on, or cut
  records off the end, and the chain alone won't show it (T-03).
- **Proof from outside the machine needs anchoring.** The Strict preset turns anchoring on, but
  it needs a private repository set up first; on the owner's setup it's off (I13).
  - Even with anchoring, records newer than the last anchor (up to an hour by default) can be
    changed by someone controlling the machine.
  - Timestamps come from the machine's clock unless a timestamp server is set (`tsa_url`).
  - Merkle checkpoints, which would make partial verification cheaper, are deferred with no
    date (ADR-0008, spec §9).
- **Auditing can be switched off** like any protection. Then only settings snapshots and
  `audit.disabled` / `audit.enabled` markers are written.

**Managing it**
- **There's no retention, rotation or redaction for the audit log.** It grows until you remove it
  yourself, and records referring to a purged conversation stay.
- **It stays on each machine.** Collecting it centrally means shipping `audit.jsonl` yourself.
  The OpenTelemetry export covers model and tool calls only, and sends only to a collector on the
  same machine (FR-AUD-5).

## Settings and governance
- **You can weaken any protection.** Each setting is switchable by design (D55). Weakening it is
  shown (badge, `privacy show`, dashboard) and audited, but not prevented. Nothing stops the
  machine's owner from loosening policy.
- **A client's own API key can name any environment** in the model it asks for
  (`cw/<environment>:<effort>`), and so pick a more open privacy level. Only run keys from
  `cairnward run` and plugins are pinned to one environment (spec §4.1, I39).
- **The configuration is plain files in your config folder.** There's no central policy, signing
  or enforcement across machines.
