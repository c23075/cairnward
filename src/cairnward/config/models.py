"""Schemas for Cairnward's configuration files (spec §4.1, §4.3, §5.0, §11).

Every file has `schema_version`; unknown keys are errors (typos shouldn't silently
weaken a setting). Built-in defaults match the spec's reference-machine choices.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairnward.config.privacy import PrivacyOverrides

SCHEMA_VERSION: Literal[1] = 1
Role = Literal["main", "fast", "vision", "embed"]
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_SECRET_REF = re.compile(r"^(keyring|env):[A-Za-z0-9_.-]{1,128}$")
_DURATION = re.compile(r"^(\d+)(s|m|h)$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_name(value: str, what: str) -> str:
    if not _NAME.match(value):
        raise ValueError(
            f"{what} '{value}' must be lowercase letters, digits, '.', '_' or '-' (max 63)"
        )
    return value


def parse_duration(value: str) -> int:
    """'90s', '30m', '2h' -> seconds."""
    m = _DURATION.match(value)
    if not m:
        raise ValueError(f"duration '{value}' must look like 90s, 30m or 2h")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


# --------------------------------------------------------------------------- cairnward.toml


class ServerConfig(_Strict):
    socket_path: Path | None = Field(
        default=None, description="Unix socket for the CLI (default: <state_dir>/cairnward.sock)"
    )
    gateway_host: str = Field(default="127.0.0.1", description="loopback only")
    gateway_port: int = Field(default=8484, ge=1, le=65535)
    dashboard_host: str = Field(default="127.0.0.1", description="loopback only")
    dashboard_port: int = Field(default=8485, ge=1, le=65535)
    egress_port: int = Field(default=8486, ge=1, le=65535, description="cairnward-egress proxy")

    @field_validator("gateway_host", "dashboard_host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        # Not switchable (spec §5.0): binding elsewhere would expose Cairnward to the network.
        if value not in LOOPBACK_HOSTS:
            raise ValueError(
                f"'{value}' is not a loopback address; the gateway and dashboard only listen "
                "on this machine (127.0.0.1, ::1 or localhost)"
            )
        return value

    @model_validator(mode="after")
    def _distinct_ports(self) -> ServerConfig:
        ports = [self.gateway_port, self.dashboard_port, self.egress_port]
        if len(set(ports)) != len(ports):
            raise ValueError("gateway_port, dashboard_port and egress_port must all differ")
        return self


class EgressConfig(_Strict):
    max_connections: int = Field(default=64, ge=1, le=4096)
    connect_timeout_s: int = Field(default=15, ge=1, le=300)
    idle_timeout_s: int = Field(default=300, ge=5, le=86400)
    approval_timeout_s: int = Field(
        default=120, ge=1, le=3600, description="an unanswered approval becomes a deny"
    )


class Defaults(_Strict):
    environment: str = "coding"
    effort: str = "medium"


class CustomPattern(_Strict):
    name: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    regex: str
    category: Literal["secret", "personal"] = "personal"

    @field_validator("regex")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression: {exc}") from None
        return value


class RedactionConfig(_Strict):
    """What the redaction scanner looks for, besides its built-in patterns (spec §5.4)."""

    custom: list[CustomPattern] = Field(default_factory=list)
    ignore: list[str] = Field(
        default_factory=list,
        description="regular expressions for matches to ignore (e.g. your own public email)",
    )

    @field_validator("ignore")
    @classmethod
    def _ignore_compiles(cls, value: list[str]) -> list[str]:
        for rx in value:
            try:
                re.compile(rx)
            except re.error as exc:
                raise ValueError(f"'{rx}' is not a valid regular expression: {exc}") from None
        return value


class AnchorConfig(_Strict):
    """External audit checkpoints (spec §5.2, D49): used when audit_anchor = "git"."""

    repo: str | None = Field(
        default=None, description="a private git repo you own, e.g. git@github.com:you/anchors.git"
    )
    branch: str = Field(default="main", pattern=r"^[A-Za-z0-9._/-]{1,100}$")
    interval_minutes: int = Field(default=60, ge=5, le=1440)


class BackupConfig(_Strict):
    """Encrypted backups with restic (spec §4.8, FR-SYNC-1/3/8)."""

    mode: Literal["none", "on_demand", "auto"] = Field(
        default="none",
        description="none: no backups; on_demand: `cairnward backup run`; auto: also by serve",
    )
    repository: str | None = Field(
        default=None,
        description="a folder for the restic repository: a second disk, NAS mount or synced "
        "folder (contents are encrypted by restic)",
    )
    interval_hours: int = Field(default=24, ge=1, le=24 * 30, description="auto mode: how often")
    keep_daily: int = Field(default=7, ge=0, le=3650)
    keep_weekly: int = Field(default=4, ge=0, le=520)
    keep_monthly: int = Field(default=12, ge=0, le=1200)
    password_command: str | None = Field(
        default=None,
        description="command printing the repository password (e.g. `pass show cairnward`); "
        "default: a generated password in the state folder",
    )
    include_research: bool = Field(default=True, description="research.db (indexed libraries)")
    include_artifacts: bool = True
    include_config: bool = Field(default=True, description="the config folder (no secrets)")


class ResearchConfig(_Strict):
    """Research workflows (spec §4.6)."""

    grobid_url: str | None = Field(
        default=None,
        description="your GROBID service on this machine, e.g. "
        "http://127.0.0.1:8070 (libraries with --metadata grobid)",
    )

    citation_engine: Literal["auto", "builtin", "citeproc-js", "pandoc"] = Field(
        default="auto",
        description="reference formatting: auto = citeproc-js if installed, else pandoc, "
        "else the built-in APA/IEEE formatter",
    )


class MainConfig(_Strict):
    """cairnward.toml"""

    schema_version: Literal[1] = SCHEMA_VERSION
    server: ServerConfig = ServerConfig()
    egress: EgressConfig = EgressConfig()
    privacy: PrivacyOverrides = PrivacyOverrides()
    defaults: Defaults = Defaults()
    redaction: RedactionConfig = RedactionConfig()
    anchor: AnchorConfig = AnchorConfig()
    research: ResearchConfig = ResearchConfig()
    backup: BackupConfig = BackupConfig()


# --------------------------------------------------------------------------- backends.toml


class Backend(_Strict):
    driver: Literal["ollama", "openai_compat"]
    url: str
    location: Literal["local", "lan", "hosted"] = "local"
    api_key: str | None = Field(
        default=None,
        description="a reference, never the key itself: 'keyring:<name>' or 'env:<VAR>'",
    )
    max_parallel: int = Field(default=1, ge=1, le=64)
    timeout_s: int = Field(default=900, ge=1)
    slot_cache: bool = Field(
        default=False,
        description="llama.cpp only: save/restore each conversation's KV cache when switching "
        "conversations (start llama-server with --slot-save-path; needs max_parallel = 1)",
    )
    fallback: str | None = Field(
        default=None,
        description="a LOCAL backend to retry on when this one is unreachable or fails (5xx)",
    )
    fallback_model: str | None = Field(
        default=None, description="model name on the fallback backend (default: the same)"
    )

    @field_validator("api_key")
    @classmethod
    def _secret_ref(cls, value: str | None) -> str | None:
        if value is not None and not _SECRET_REF.match(value):
            raise ValueError(
                "api_key must be a reference like 'keyring:openrouter' or 'env:OPENROUTER_KEY'; "
                "keys are never stored in config files"
            )
        return value

    @model_validator(mode="after")
    def _url_matches_location(self) -> Backend:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"url '{self.url}' must be an http(s) URL with a host")
        host = parsed.hostname
        if self.location == "local" and host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"location 'local' requires a loopback URL (got host '{host}'); "
                "use location = 'lan' or 'hosted' for other machines"
            )
        if self.location == "lan":
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                ip = None  # a LAN host name; can't check further here
            if ip is not None and not ip.is_private:
                raise ValueError(f"location 'lan' requires a private address (got '{host}')")
        if self.location == "hosted" and parsed.scheme != "https":
            raise ValueError("hosted backends must use https")
        if self.slot_cache and (self.driver != "openai_compat" or self.max_parallel != 1):
            raise ValueError(
                "slot_cache needs driver = 'openai_compat' (llama-server) and max_parallel = 1"
            )
        return self


class RoleModel(_Strict):
    backend: str
    model: str


def _default_backends() -> dict[str, Backend]:
    return {"local-ollama": Backend(driver="ollama", url="http://127.0.0.1:11434")}


def _default_roles() -> dict[Role, RoleModel]:
    # Spec D61: measured on the reference machine (docs/bench/).
    return {
        "main": RoleModel(backend="local-ollama", model="qwen3:30b"),
        "fast": RoleModel(backend="local-ollama", model="qwen3:1.7b"),
        "embed": RoleModel(backend="local-ollama", model="qwen3-embedding:0.6b"),
    }


class BackendsConfig(_Strict):
    """backends.toml"""

    schema_version: Literal[1] = SCHEMA_VERSION
    backends: dict[str, Backend] = Field(default_factory=_default_backends)
    roles: dict[Role, RoleModel] = Field(default_factory=_default_roles)
    fallback: list[str] = Field(
        default_factory=list, description="local backends to try if a backend is down"
    )

    @field_validator("backends")
    @classmethod
    def _names(cls, value: dict[str, Backend]) -> dict[str, Backend]:
        for name in value:
            _check_name(name, "backend name")
        return value

    @model_validator(mode="after")
    def _references(self) -> BackendsConfig:
        for role, rm in self.roles.items():
            if rm.backend not in self.backends:
                raise ValueError(f"roles.{role}: unknown backend '{rm.backend}'")
        for name in self.fallback:
            if name not in self.backends:
                raise ValueError(f"fallback: unknown backend '{name}'")
            if self.backends[name].location != "local":
                raise ValueError(
                    f"fallback: '{name}' is not local; fallback only uses local backends"
                )
        return self


# --------------------------------------------------------------------------- clients.toml

AgentProfile = Literal["aider", "qwen-code", "opencode", "goose", "generic"]
ChatProfile = Literal["open-webui", "jan", "librechat", "anythingllm", "generic"]


class Client(_Strict):
    kind: Literal["chat", "agent"]
    profile: str = "generic"
    default_environment: str | None = None
    enabled: bool = True
    backend: str | None = Field(default=None, description="this client's backend (all roles)")
    models: dict[Role, str] = Field(default_factory=dict, description="this client's models")

    @model_validator(mode="after")
    def _profile_matches_kind(self) -> Client:
        allowed = AgentProfile if self.kind == "agent" else ChatProfile
        if self.profile not in get_args(allowed):
            raise ValueError(f"profile '{self.profile}' is not a known {self.kind} profile")
        return self


class ClientsConfig(_Strict):
    """clients.toml (API keys live in the state store, not here)"""

    schema_version: Literal[1] = SCHEMA_VERSION
    clients: dict[str, Client] = Field(default_factory=dict)

    @field_validator("clients")
    @classmethod
    def _names(cls, value: dict[str, Client]) -> dict[str, Client]:
        for name in value:
            _check_name(name, "client name")
        return value


# --------------------------------------------------------------------------- effort.toml


class SubagentLimits(_Strict):
    max: int = Field(default=0, ge=0, le=32)
    max_parallel: int = Field(default=1, ge=1, le=32)
    backend: str | None = None
    effort: str | None = None


class EffortLevel(_Strict):
    model_role: Literal["main", "fast"] | None = Field(default=None, description="or set model")
    model: str | None = None
    backend: str | None = None
    thinking: bool = False
    thinking_budget_tokens: int | None = Field(default=None, ge=0)
    max_tool_steps: int = Field(default=10, ge=0, le=1000)
    subagents: SubagentLimits = SubagentLimits()
    context_tokens: int | None = Field(default=None, ge=512, le=1_048_576)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    top_k: int | None = Field(default=None, ge=1)
    min_p: float | None = Field(default=None, ge=0, le=1)
    wall_clock_limit: str | None = Field(default=None, description="e.g. 30m")
    max_tokens_per_request: int | None = Field(default=None, ge=1)
    skills: list[str] = Field(default_factory=list)
    hidden: bool = False
    order: int = 100

    @field_validator("subagents", mode="before")
    @classmethod
    def _int_subagents(cls, value: Any) -> Any:
        return {"max": value} if isinstance(value, int) else value

    @field_validator("wall_clock_limit")
    @classmethod
    def _duration(cls, value: str | None) -> str | None:
        if value is not None:
            parse_duration(value)
        return value

    @model_validator(mode="after")
    def _model_or_role(self) -> EffortLevel:
        if (self.model_role is None) == (self.model is None):
            raise ValueError("set exactly one of model_role or model")
        return self


def default_effort_levels() -> dict[str, EffortLevel]:
    """Spec §4.2 with D61 models."""
    return {
        "low": EffortLevel(model_role="fast", thinking=False, max_tool_steps=3, order=10),
        "medium": EffortLevel(
            model_role="main",
            thinking=False,
            max_tool_steps=10,
            subagents=SubagentLimits(max=2),
            order=20,
        ),
        "high": EffortLevel(
            model_role="main",
            thinking=True,
            thinking_budget_tokens=4096,
            max_tool_steps=25,
            subagents=SubagentLimits(max=4),
            order=30,
        ),
    }


class EffortConfig(_Strict):
    """effort.toml: levels here replace built-ins of the same name and add new ones."""

    schema_version: Literal[1] = SCHEMA_VERSION
    effort: dict[str, EffortLevel] = Field(default_factory=dict)

    @field_validator("effort")
    @classmethod
    def _names(cls, value: dict[str, EffortLevel]) -> dict[str, EffortLevel]:
        for name in value:
            _check_name(name, "effort level")
        return value


# --------------------------------------------------------------------------- environments/*.toml


class RunBudget(_Strict):
    """Limits for one `cairnward run` (FR-GW-4); unset = unlimited."""

    max_requests: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_minutes: float | None = Field(default=None, gt=0)


class EnvironmentConfig(_Strict):
    """environments/<name>.toml (the file name is the environment's name)"""

    schema_version: Literal[1] = SCHEMA_VERSION
    description: str = ""
    workspace: list[Path] = Field(default_factory=list, description="folders agents may write")
    backend: str | None = Field(default=None, description="override the backend for all roles")
    models: dict[Role, str] = Field(default_factory=dict)
    effort_default: str | None = None
    context_tokens: int = Field(default=8192, ge=512, le=1_048_576, description="spec D60")
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(
        default_factory=list, description="MCP servers (mcp.toml) offered in this environment"
    )
    skills: list[str] = Field(default_factory=list)
    research_sources: list[
        Literal["openalex", "semantic-scholar", "arxiv", "crossref", "pubmed"]
    ] = Field(
        default_factory=list,
        description="online academic sources this environment may "
        "search (opt-in; each request is still checked against its network setting)",
    )
    citation_style: str = "apa"
    vault_retention_days: int | None = Field(default=None, ge=1)
    run_budget: RunBudget = RunBudget()
    context_overflow: Literal["warn", "reject"] = Field(
        default="warn", description="a prompt larger than context_tokens: warn, or refuse it"
    )
    privacy: PrivacyOverrides = PrivacyOverrides()

    @field_validator("workspace", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("workspace")
    @classmethod
    def _expand(cls, value: list[Path]) -> list[Path]:
        return [p.expanduser() for p in value]


# --------------------------------------------------------------------------- mcp.toml

_MCP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
ToolSetting = Literal["allow", "ask", "deny"]


class McpPermissions(_Strict):
    """What a third-party MCP server may do (its manifest), within the environment's
    privacy settings. Paths may use {workspace} (the environment's first workspace) and
    {data} (the server's own folder, also its HOME)."""

    network: list[str] = Field(
        default_factory=list, description="hosts it may reach (through egress), e.g. api.x.org"
    )
    read: list[str] | None = Field(
        default=None,
        description="if set, only these folders (plus system folders) are readable; "
        "otherwise everything except secrets and Cairnward's own folders",
    )
    write: list[str] = Field(
        default_factory=list, description="writable folders: inside {workspace} or {data}"
    )
    env: list[str] = Field(
        default_factory=list, description="environment variables passed through from yours"
    )


class McpServer(_Strict):
    description: str = ""
    command: list[str] = Field(min_length=1, description="stdio server command and arguments")
    env: dict[str, str] = Field(default_factory=dict, description="fixed environment variables")
    cwd: str = Field(default="{data}", description="working folder: {data} or {workspace}")
    version: str = Field(default="", description="the version you reviewed (shown, audited)")
    pin_file: Path | None = Field(
        default=None, description="a file whose SHA-256 must match `sha256` before starting"
    )
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    permissions: McpPermissions = McpPermissions()
    default_tool: ToolSetting | None = Field(
        default=None,
        description="policy for its tools: allow / ask / deny; unset = by the approvals level",
    )
    tools: dict[str, ToolSetting] = Field(default_factory=dict, description="per-tool policy")
    call_timeout_s: int = Field(default=120, ge=1, le=3600)

    @model_validator(mode="after")
    def _pin_pair(self) -> McpServer:
        if (self.pin_file is None) != (self.sha256 is None):
            raise ValueError("pin_file and sha256 go together")
        if self.cwd not in ("{data}", "{workspace}"):
            raise ValueError("cwd must be {data} or {workspace}")
        return self


class McpConfig(_Strict):
    """mcp.toml: third-party MCP servers, offered to clients through Cairnward (spec §4.2)"""

    schema_version: Literal[1] = SCHEMA_VERSION
    servers: dict[str, McpServer] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def _names(cls, value: dict[str, McpServer]) -> dict[str, McpServer]:
        for name in value:
            if name == "cairnward":
                raise ValueError("'cairnward' is reserved for Cairnward's own tools")
            if not _MCP_NAME.match(name):
                raise ValueError(
                    f"MCP server name '{name}' must be lowercase letters, digits or '-' (max 31)"
                )
        return value


def builtin_environments() -> dict[str, EnvironmentConfig]:
    """Templates from spec §4.3; a file with the same name replaces one."""
    return {
        "coding": EnvironmentConfig(
            description="Coding agents in the current project",
            tools=["fs", "shell", "git"],
            effort_default="medium",
        ),
        "litreview-offline": EnvironmentConfig(
            description="Literature review with no network",
            tools=["rag", "pdf", "zotero"],
            skills=["lit-review", "citation-check", "references"],
            effort_default="high",
            privacy=PrivacyOverrides(network="off", hosted_backends="deny"),
        ),
        "litreview-online": EnvironmentConfig(
            description="Literature review with academic APIs (approval per new host)",
            tools=["rag", "pdf", "zotero", "scholar"],
            skills=["lit-review", "citation-check", "references"],
            effort_default="high",
            research_sources=["openalex", "semantic-scholar", "arxiv", "crossref", "pubmed"],
            privacy=PrivacyOverrides(
                network="ask",
                network_allow_hosts=[
                    "export.arxiv.org",
                    "api.semanticscholar.org",
                    "api.openalex.org",
                    "eutils.ncbi.nlm.nih.gov",
                    "api.crossref.org",
                ],
            ),
        ),
        "scratch": EnvironmentConfig(
            description="Experiments with no protections in the way",
            effort_default="low",
            privacy=PrivacyOverrides(preset="open"),
        ),
    }
