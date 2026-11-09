import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from cairnward.audit import AuditLog
from cairnward.config import Config, load_config
from cairnward.config.paths import Paths
from cairnward.policy import (
    Action,
    ApprovalAnswer,
    ApprovalRequest,
    PolicyDenied,
    PolicyEngine,
    ScriptedApprover,
    TerminalApprover,
    host_matches,
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def config(
    paths: Paths, main: str = "", env: str | None = None, backends: str | None = None
) -> Config:
    write(paths.config_dir / "cairnward.toml", main)
    if env is not None:
        write(paths.config_dir / "environments" / "e.toml", env)
    if backends is not None:
        write(paths.config_dir / "backends.toml", backends)
    return load_config(paths)


def yes(scope: str = "once") -> ScriptedApprover:
    return ScriptedApprover(lambda req: ApprovalAnswer(True, scope))  # type: ignore[arg-type]


def no() -> ScriptedApprover:
    return ScriptedApprover(lambda req: ApprovalAnswer(False))


def act(kind: str, target: str, **kw: object) -> Action:
    return Action(kind=kind, target=target, environment=kw.pop("environment", "e"), **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ host matching


@pytest.mark.parametrize(
    ("host", "pattern", "ok"),
    [
        ("export.arxiv.org", "export.arxiv.org", True),
        ("EXPORT.arxiv.org", "export.arxiv.org", True),
        ("api.arxiv.org", "*.arxiv.org", True),
        ("arxiv.org", "*.arxiv.org", False),
        ("evilarxiv.org", "*.arxiv.org", False),
        ("pypi.org:443", "pypi.org", True),
        ("pypi.org:443", "pypi.org:443", True),
        ("pypi.org:22", "pypi.org:443", False),
        ("pypi.org", "pypi.org:443", False),
        # IPv6, with or without brackets and port (egress's targets are "[v6]:port")
        ("[::1]:443", "[::1]", True),
        ("[::1]:443", "::1", True),
        ("[::1]:443", "[::1]:443", True),
        ("[::1]:443", "[::1]:80", False),
        ("::1", "::1", True),
        ("::1", "[::1]", True),
        ("[2001:db8::1]:443", "2001:db8::1", True),
        ("[2001:db8::2]:443", "2001:db8::1", False),
    ],
)
def test_host_matches(host: str, pattern: str, ok: bool) -> None:
    assert host_matches(host, pattern) is ok


# ------------------------------------------------------------------ network


@pytest.mark.parametrize(
    ("privacy", "host", "expected"),
    [
        ('preset = "open"', "anything.example", "allow"),
        ('network = "off"', "export.arxiv.org", "deny"),
        (
            'network = "allowlist"\nnetwork_allow_hosts = ["*.arxiv.org"]',
            "export.arxiv.org",
            "allow",
        ),
        ('network = "allowlist"\nnetwork_allow_hosts = ["*.arxiv.org"]', "evil.example", "deny"),
        ('network = "ask"\nnetwork_allow_hosts = ["*.arxiv.org"]', "export.arxiv.org", "allow"),
    ],
)
def test_network_modes(paths: Paths, privacy: str, host: str, expected: str) -> None:
    engine = PolicyEngine(config(paths, env=f"[privacy]\n{privacy}\n"), approver=no())
    assert engine.check(act("net.connect", host)).decision == expected


def test_network_ask_always_asks_even_with_approvals_off(paths: Paths) -> None:
    approver = yes()
    cfg = config(paths, env='[privacy]\nnetwork = "ask"\napprovals = "off"\n')
    d = PolicyEngine(cfg, approver=approver).check(act("net.connect", "new.example"))
    assert d.allowed and d.asked and len(approver.asked) == 1


def test_denied_when_nobody_can_be_asked(paths: Paths) -> None:
    cfg = config(paths, env='[privacy]\nnetwork = "ask"\n')
    d = PolicyEngine(cfg).check(act("net.connect", "new.example"))  # default DenyApprover
    assert not d.allowed and d.approved_by == "no-approver"


def test_remembered_for_environment_but_not_elsewhere(paths: Paths) -> None:
    approver = yes("environment")
    cfg = config(paths, env='[privacy]\nnetwork = "ask"\n')
    engine = PolicyEngine(cfg, approver=approver)
    assert engine.check(act("net.connect", "new.example")).allowed
    second = engine.check(act("net.connect", "new.example"))
    assert second.allowed and second.scope == "environment" and len(approver.asked) == 1
    engine.check(act("net.connect", "other.example"))
    assert len(approver.asked) == 2


def test_remembered_for_conversation_only(paths: Paths) -> None:
    approver = yes("conversation")
    cfg = config(paths, env='[privacy]\nnetwork = "ask"\n')
    engine = PolicyEngine(cfg, approver=approver)
    engine.check(act("net.connect", "h.example", conversation_id="c1"))
    engine.check(act("net.connect", "h.example", conversation_id="c1"))
    engine.check(act("net.connect", "h.example", conversation_id="c2"))
    assert len(approver.asked) == 2


def test_isolation_off_marks_network_decisions_advisory(paths: Paths) -> None:
    cfg = config(paths, env='[privacy]\nisolation = "off"\nnetwork = "allowlist"\n')
    d = PolicyEngine(cfg).check(act("net.connect", "x.example"))
    assert not d.allowed and d.advisory


# ------------------------------------------------------------------ model backends

BACKENDS = """
[backends.local-ollama]
driver = "ollama"
url = "http://127.0.0.1:11434"
[backends.cloud]
driver = "openai_compat"
url = "https://openrouter.ai/api/v1"
location = "hosted"
[backends.gpu]
driver = "openai_compat"
url = "http://10.0.0.5:8000/v1"
location = "lan"
"""


@pytest.mark.parametrize(
    ("privacy", "backend", "expected", "asked"),
    [
        ('preset = "strict"', "local-ollama", "allow", False),
        ('preset = "strict"', "cloud", "deny", False),
        ('preset = "balanced"', "cloud", "allow", True),  # hosted_backends = ask
        ('preset = "open"', "cloud", "allow", False),
        ('preset = "open"\nnetwork = "off"', "cloud", "deny", False),
        ('preset = "strict"\nnetwork_allow_hosts = ["10.0.0.5:8000"]', "gpu", "allow", False),
        ('preset = "strict"', "gpu", "deny", False),
        ('preset = "open"', "missing", "deny", False),
    ],
)
def test_model_backends(
    paths: Paths, privacy: str, backend: str, expected: str, asked: bool
) -> None:
    approver = yes()
    cfg = config(paths, env=f"[privacy]\n{privacy}\n", backends=BACKENDS)
    d = PolicyEngine(cfg, approver=approver).check(act("model.call", backend))
    assert d.decision == expected and d.asked is asked


# ------------------------------------------------------------------ files and exec


def test_files_inside_and_outside_workspace(paths: Paths, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    cfg = config(paths, env=f'workspace = "{ws.as_posix()}"\n[privacy]\napprovals = "tiered"\n')
    approver = no()
    engine = PolicyEngine(cfg, approver=approver)
    assert engine.check(act("fs.write", str(ws / "a.py"))).allowed
    assert engine.check(act("fs.write", str(paths.data_dir / "artifacts/x.md"))).allowed
    assert not engine.check(act("fs.write", str(tmp_path / "elsewhere.txt"))).allowed
    assert not engine.check(act("fs.read", "/etc/hostname")).allowed  # medium -> asked -> no
    assert not engine.check(act("fs.write", str(ws / ".." / "escape.txt"))).allowed
    assert len(approver.asked) == 3


def test_approvals_levels(paths: Paths) -> None:
    """Medium-risk reads: tiered asks, high_only and off allow."""
    for level, asked in (("tiered", True), ("high_only", False), ("off", False)):
        approver = yes()
        cfg = config(paths, env=f'[privacy]\napprovals = "{level}"\n')
        PolicyEngine(cfg, approver=approver).check(act("fs.read", "/etc/hostname"))
        assert bool(approver.asked) is asked, level


def test_high_risk_never_remembered(paths: Paths, tmp_path: Path) -> None:
    approver = yes("environment")
    engine = PolicyEngine(config(paths, env=""), approver=approver)
    outside = str(tmp_path / "outside.txt")
    engine.check(act("fs.write", outside))
    engine.check(act("fs.write", outside))
    assert len(approver.asked) == 2
    assert approver.asked[0].rememberable is False


def test_exec_depends_on_isolation(paths: Paths) -> None:
    approver = no()
    on = PolicyEngine(config(paths, env='[privacy]\nisolation = "on"\n'), approver=approver)
    assert on.check(act("exec", "pytest")).allowed
    off = PolicyEngine(config(paths, env='[privacy]\nisolation = "off"\n'), approver=approver)
    assert not off.check(act("exec", "pytest")).allowed  # high risk, asked, refused


# ------------------------------------------------------------------ user-only actions


def test_user_only_from_model_always_asks_with_provenance(paths: Paths) -> None:
    approver = yes("environment")
    cfg = config(paths, env='[privacy]\npreset = "open"\n')  # approvals off
    engine = PolicyEngine(cfg, approver=approver)
    action = act("sync.change", "job:backup", provenance=("web page example.com",))
    assert engine.check(action).allowed
    assert engine.check(action).allowed
    assert len(approver.asked) == 2  # never remembered
    req = approver.asked[0]
    assert req.provenance == ("web page example.com",) and not req.rememberable


def test_user_only_from_user_follows_approvals(paths: Paths) -> None:
    approver = yes()
    cfg = config(paths, env='[privacy]\npreset = "open"\n')
    d = PolicyEngine(cfg, approver=approver).check(act("encrypt", "conv:1", initiated_by="user"))
    assert d.allowed and not d.asked


# ------------------------------------------------------------------ guard and audit


@pytest.fixture
def audit(paths: Paths) -> Iterator[AuditLog]:
    with AuditLog.open(paths) as log:
        yield log


def test_guard_records_allowed_action(paths: Paths, audit: AuditLog) -> None:
    engine = PolicyEngine(config(paths, env='[privacy]\npreset = "open"\n'), audit=audit)
    with engine.guard(act("net.connect", "x.example", actor="agent:aider"), params="GET /") as eff:
        eff.result("200 OK")
    pre, post = audit.records()
    assert (pre.action, pre.phase, pre.decision, post.outcome) == (
        "net.connect",
        "pre",
        "allow",
        "ok",
    )
    assert pre.details["reason"] == "network = open: outbound connections allowed"
    assert pre.actor == "agent:aider"
    assert audit.verify().ok


def test_guard_records_denial_and_raises(paths: Paths, audit: AuditLog) -> None:
    engine = PolicyEngine(config(paths, env='[privacy]\nnetwork = "off"\n'), audit=audit)
    ran = False
    with pytest.raises(PolicyDenied), engine.guard(act("net.connect", "x.example")):
        ran = True
    assert not ran
    (rec,) = audit.records()
    assert (rec.phase, rec.decision) == ("event", "deny")


def test_guard_without_audit(paths: Paths) -> None:
    engine = PolicyEngine(config(paths, env='[privacy]\npreset = "open"\n'))
    with engine.guard(act("net.connect", "x.example")):
        pass


# ------------------------------------------------------------------ terminal approver


def _req(**kw: object) -> ApprovalRequest:
    base: dict[str, object] = dict(
        kind="net.connect",
        target="h",
        risk="medium",
        reason="r",
        environment="e",
        conversation_id="c",
        client=None,
        initiated_by="model",
    )
    base.update(kw)
    return ApprovalRequest(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("reply", "granted", "scope"),
    [
        ("y\n", True, "once"),
        ("c\n", True, "conversation"),
        ("e\n", True, "environment"),
        ("n\n", False, "once"),
        ("\n", False, "once"),
        ("", False, "once"),
    ],
)
def test_terminal_approver(reply: str, granted: bool, scope: str) -> None:
    out = io.StringIO()
    ans = TerminalApprover(io.StringIO(reply), out).request(_req())
    assert (ans.granted, ans.scope) == (granted, scope)
    assert "Approval needed" in out.getvalue()


def test_terminal_approver_not_rememberable_and_provenance() -> None:
    out = io.StringIO()
    ans = TerminalApprover(io.StringIO("e\n"), out).request(
        _req(rememberable=False, provenance=("read paper.pdf p.4",))
    )
    assert not ans.granted  # 'e' isn't offered for non-rememberable requests
    assert "read paper.pdf p.4" in out.getvalue() and "[y] yes, once" in out.getvalue()


# ------------------------------------------------------------------ CLI


def test_cli_explain(paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    from cairnward.cli import main

    base = ["--config-dir", str(paths.config_dir), "policy", "explain"]
    paths.environments_dir.mkdir(parents=True, exist_ok=True)
    (paths.environments_dir / "offline.toml").write_text('[privacy]\nnetwork = "off"\n')
    (paths.environments_dir / "online.toml").write_text('[privacy]\nnetwork = "ask"\n')
    assert main([*base, "net.connect", "pypi.org", "--env", "offline"]) == 0
    assert capsys.readouterr().out.startswith("DENY")
    assert main([*base, "net.connect", "new.example", "--env", "online"]) == 0
    assert capsys.readouterr().out.startswith("ASK")
    assert main([*base, "teleport", "x"]) == 2
