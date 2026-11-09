# Cairnward in UML

Written 2026-11-09 from the code on `main`. Class and method names are the real ones. The
diagrams are Mermaid, which GitHub renders, so they can be edited like the rest of the docs. The
prose description of the architecture is in [spec.md §3](spec.md#3-architecture); what the
protections don't do is in [limitations.md](limitations.md).

## 1. Deployment: processes and sandboxes (isolated mode)

Each box with a dashed border is an OS sandbox (srt: bubblewrap and seccomp). Only the outer
process has network access.

```mermaid
flowchart TB
    subgraph host["Your machine (one OS user)"]
        direction TB
        clients["Chat clients<br/>Open WebUI, Jan, LibreChat"]
        subgraph outer["Outer process: cairnward serve"]
            fwd["Forwarders<br/>127.0.0.1:8484 gateway<br/>127.0.0.1:8485 dashboard<br/>client socket"]
            egress["EgressProxy<br/>127.0.0.1:8486"]
            ctl["Egress control API<br/>(Unix socket)"]
            bg["Background jobs<br/>restic backups, audit anchors,<br/>replication, OpenTelemetry export"]
        end
        subgraph core["Core: srt sandbox, no network device, VM sockets refused"]
            gw["Gateway /v1, /mcp"]
            mgmt["Management API<br/>cairnward.sock"]
            dash["Dashboard"]
            engine["PolicyEngine<br/>ApprovalInbox"]
            stores[("audit.db + audit.jsonl<br/>vault.db, keys.db")]
        end
        subgraph agent["Agent run: srt or OpenShell sandbox"]
            ag["Coding agent<br/>Aider, opencode, Qwen Code"]
        end
        subgraph mcpbox["MCP server / plugin: srt sandbox each"]
            mcp["Tool server"]
        end
        models["Local model server<br/>Ollama, llama.cpp"]
    end
    internet(("Allowed hosts:<br/>hosted models,<br/>remote MCP, sites"))

    clients -->|"OpenAI API + client key"| fwd
    ag -->|"run key"| fwd
    fwd --> gw
    fwd --> dash
    gw --> engine
    gw --> stores
    gw -->|"local backends,<br/>allow-listed"| models
    ag -->|"HTTP(S)_PROXY + session token"| egress
    mcp -->|"declared hosts only"| egress
    gw <-->|"tool calls over a socket"| mcp
    core -->|"its own calls,<br/>trusted session"| egress
    ctl <-->|"sessions, launches,<br/>backups, settings"| core
    egress --> internet

    style core stroke-dasharray: 5 5
    style agent stroke-dasharray: 5 5
    style mcpbox stroke-dasharray: 5 5
```

With isolation off, the outer process and the core are one process. Agents and MCP servers in
an environment with isolation on are still sandboxed.

## 2. Classes: the core and its decisions

Main classes and how they hold each other. Fields and methods are a selection, not the full
interfaces.

```mermaid
classDiagram
    direction LR

    class Config {
        +Paths paths
        +MainConfig main
        +environments
        +privacy(environment) EffectivePrivacy
    }
    class EffectivePrivacy {
        +settings
        +network_allow_hosts
        +warnings
        +value(name) str
    }
    class PolicyEngine {
        +Config config
        +Approver approver
        +AuditLog audit
        +check(Action) Decision
        +rule_for(Action) Rule
        +audit_pre(Decision) int
        +audit_post(Action, pre_seq)
    }
    class Action {
        +kind
        +target
        +environment
        +client
        +run_id
    }
    class Decision {
        +decision allow|ask|deny
        +risk
        +reason
        +allowed bool
    }
    class Approver {
        <<interface>>
        +ask(ApprovalRequest) ApprovalAnswer
    }
    class ApprovalInbox
    class RemoteApprover
    class TerminalApprover
    class DenyApprover
    class AuditLog {
        +append(action, ...) AuditRecord
        +verify() VerifyResult
        +head()
    }
    class AuditRecord {
        +seq
        +action
        +params_digest
        +prev_hash
        +hash
    }
    class Vault {
        +record_exchange()
        +lock(id, secret)
        +unlock(id, secret, minutes)
        +soft_delete(id)
        +purge(id)
    }
    class GatewayDeps {
        +Config config
        +PolicyEngine engine
        +Backends backends
        +ClientKeys keys
        +RunKeys run_keys
        +Vault vault
        +Scheduler scheduler
        +McpHub mcp
    }
    class Backends {
        +get(backend, environment) Driver
    }
    class Driver {
        <<interface>>
        +chat(ChatRequest)
        +chat_stream(ChatRequest)
        +embed(model, inputs)
    }
    class OllamaDriver
    class OpenAICompatDriver
    class Scheduler {
        +slot(backend, conversation)
    }
    class ClientKeys {
        +identify(key) client
    }
    class RunKeys {
        +issue(client, run_id, budget)
        +identify(key) (client, run_id)
        +revoke_run(run_id)
    }
    class McpHub {
        +Config cfg
        +PolicyEngine engine
        +Launcher launcher
        +PinStore pins
    }
    class Launcher {
        <<interface>>
    }
    class LocalLauncher
    class BridgeLauncher
    class RemoteLauncher
    class Connection {
        +initialize()
    }
    class EgressProxy {
        +Sessions sessions
        +PolicyEngine engine
        +start()
    }
    class Sessions {
        +create(Session) token
        +revoke_run(run_id)
        +lookup(header) Session
    }
    class Session {
        +environment
        +client
        +run_id
        +trusted_core
        +inspect
    }
    class Inspector {
        +run(client, upstream)
    }
    class Issuer {
        +context(host) SSLContext
    }

    Config ..> EffectivePrivacy : resolves
    PolicyEngine --> Config
    PolicyEngine --> Approver
    PolicyEngine --> AuditLog
    PolicyEngine ..> Action : checks
    PolicyEngine ..> Decision : returns
    Approver <|.. ApprovalInbox
    Approver <|.. RemoteApprover
    Approver <|.. TerminalApprover
    Approver <|.. DenyApprover
    AuditLog *-- AuditRecord
    GatewayDeps o-- PolicyEngine
    GatewayDeps o-- Backends
    GatewayDeps o-- Vault
    GatewayDeps o-- Scheduler
    GatewayDeps o-- ClientKeys
    GatewayDeps o-- RunKeys
    GatewayDeps o-- McpHub
    Backends --> Driver
    Driver <|.. OllamaDriver
    Driver <|.. OpenAICompatDriver
    McpHub --> PolicyEngine
    McpHub --> Launcher
    McpHub --> Connection
    Launcher <|.. LocalLauncher
    Launcher <|.. BridgeLauncher
    Launcher <|.. RemoteLauncher
    EgressProxy --> Sessions
    Sessions *-- Session
    EgressProxy --> PolicyEngine
    EgressProxy ..> Inspector : TLS inspection
    EgressProxy ..> Issuer : local CA
```

## 3. Sequence: a chat request through the gateway

A request from a chat client, with the protections on (Balanced or Strict).

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Client as Chat client
    participant GW as Gateway /v1
    participant Keys as ClientKeys
    participant Eng as PolicyEngine
    participant Inbox as ApprovalInbox
    participant Red as Redaction
    participant Sch as Scheduler
    participant Drv as Driver
    participant Model as Model server
    participant Vault
    participant Audit as AuditLog

    Client->>GW: POST /v1/chat/completions (model cw/coding:medium, key)
    GW->>Keys: identify(key)
    Keys-->>GW: client
    GW->>GW: resolve virtual model to backend, model, effort
    GW->>Eng: check(Action model.call)
    alt rule says ask (e.g. a hosted backend)
        Eng->>Inbox: ask(request)
        Inbox-->>User: approval prompt (dashboard or CLI)
        User-->>Inbox: yes / no
        Inbox-->>Eng: answer (no reply in time = no)
    end
    Eng-->>GW: Decision
    alt denied
        GW->>Audit: deny record
        GW-->>Client: 403 with the reason
    else allowed
        GW->>Red: scan outgoing text
        Red-->>GW: findings (block, ask or warn)
        GW->>Audit: pre record (prompt as a digest)
        GW->>Sch: slot(backend, conversation)
        Sch-->>GW: turn (cached conversation first)
        GW->>Drv: chat_stream(request)
        Drv->>Model: request
        Model-->>Drv: tokens
        Drv-->>GW: chunks
        Note over GW: StreamGuard holds a new model's first answer<br/>until it's clear it isn't reasoning (I5)
        GW-->>Client: streamed reply
        GW->>Vault: record_exchange (encrypted per conversation)
        GW->>Audit: post record (tokens, time, result digest)
    end
```

## 4. Sequence: a sandboxed agent run going out to the network

`cairnward run -- aider` in an environment with `network = "ask"`.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant CLI as cairnward run
    participant Mgmt as Management API
    participant Ctl as Egress control
    participant SB as Sandbox (srt)
    participant Agent
    participant GW as Gateway
    participant Egr as EgressProxy
    participant Eng as PolicyEngine
    participant Site as Allowed host
    participant Audit as AuditLog

    User->>CLI: cairnward run --env coding -- aider
    CLI->>Mgmt: POST /v1/runs
    Mgmt->>Mgmt: RunKeys.issue (short-lived run key, budget)
    Mgmt->>Ctl: create Session (environment, run, inspect if given the CA)
    Ctl-->>Mgmt: egress token
    Mgmt->>Audit: run.start
    Mgmt-->>CLI: run key, egress token, ports
    CLI->>CLI: snapshot the workspace, plan the sandbox
    CLI->>SB: start the agent (workspace writable, home hidden)
    SB->>Agent: exec
    Agent->>GW: model calls with the run key
    GW-->>Agent: answers (as in diagram 3, budget enforced)
    Agent->>Egr: CONNECT pypi.org:443 (token)
    Egr->>Eng: check(Action net.connect)
    alt host not yet allowed
        Eng-->>User: approval prompt
        User-->>Eng: yes, for this run
    end
    Eng-->>Egr: allow
    Egr->>Egr: resolve once, refuse local or metadata addresses
    Egr->>Audit: pre record
    Egr->>Site: tunnel (or TLS inspection with method and path rules)
    Site-->>Agent: response
    Egr->>Audit: post record (bytes, duration)
    Agent-->>CLI: exits
    CLI->>CLI: snapshot again (runs diff / revert)
    CLI->>Mgmt: POST /v1/runs/{id}/end
    Mgmt->>Ctl: revoke the run's egress session
    Mgmt->>Mgmt: revoke the run key
    Mgmt->>Audit: run.end (exit code, files changed)
```

## 5. State: a conversation in the vault

With `crypto_shred = "on"`: each conversation has its own key, and purging destroys it.

```mermaid
stateDiagram-v2
    [*] --> Unlocked : first exchange recorded
    Unlocked --> Locked : lock(passphrase)
    Locked --> Unlocked : unlock(passphrase, minutes)
    Unlocked --> Locked : relock (after the minutes, or by hand)
    Unlocked --> Trash : soft_delete
    Locked --> Trash : soft_delete
    Trash --> Unlocked : restore
    Trash --> Purged : purge, or after 30 days
    Unlocked --> Purged : retention (vault_retention_days)
    Purged --> [*]

    note right of Purged
        Key destroyed, content deleted,
        a tombstone kept for audit and sync.
        Old backups too, with backup_purge.
    end note
    note left of Locked
        Key wrapped with the passphrase.
        Never sent to a hosted backend.
    end note
```
