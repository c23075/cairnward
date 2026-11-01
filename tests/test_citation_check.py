"""Citation and quotation checks (FR-RES-5)."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.cli import main
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.policy import PolicyEngine
from cairnward.research import InProcessEmbedder, Research
from cairnward.research.ask import run_ask
from cairnward.research.check import check_draft, normalise, quotations
from tests.fakes.backends import FakeOllama
from tests.fakes.pdf import make_pdf

LDQ, RDQ, RSQ, DASH = chr(0x201C), chr(0x201D), chr(0x2019), chr(0x2013)


def test_normalise_and_find_quotes() -> None:
    assert normalise(f"  The  {LDQ}effect{RDQ} didn{RSQ}t\nhold {DASH} at all­ ") == (
        'the "effect" didn\'t hold - at all'
    )
    text = f'They found {LDQ}longer sleep improved memory{RDQ} [2] and "a second claim here" .'
    assert quotations(text) == [("longer sleep improved memory", [2]),
                                ("a second claim here", [])]  # fmt: skip


@pytest.fixture
async def env(tmp_path: Path) -> Any:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    fake = FakeOllama()
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fake.handler))
    audit = AuditLog.open(paths)
    engine = PolicyEngine(cfg, audit=audit)
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "sleep.pdf").write_bytes(make_pdf(
        [[f"Line {i}: participants who slept longer had better working memory." for i in range(25)],
         ["The effect held after con-", "trolling for age and caffeine intake."] * 12],
        title="Sleep and Memory"))  # fmt: skip
    with Research.open(paths) as r:
        r.create("thesis", papers, "litreview-offline")
        await r.index("thesis", InProcessEmbedder(cfg, engine, backends,
                                                  environment="litreview-offline"))  # fmt: skip
    yield cfg, engine, backends, fake, paths
    audit.close()
    await backends.aclose()


async def test_quotes_in_answers(env: Any) -> None:
    cfg, engine, backends, fake, _ = env
    fake.reply = (
        f"Yes: {LDQ}participants who slept longer had better working memory{RDQ} [1]. "
        'They also say "sleep doubles intelligence in all adults" [1].'
    )
    a = await run_ask(cfg, engine, backends, "thesis", "Does sleep help working memory?", k=3)
    assert len(a.quote_problems) == 1 and "sleep doubles intelligence" in a.quote_problems[0]
    assert "was not found in the papers" in a.render()


def test_draft_check(env: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str],
                     monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    _, _, _, _, paths = env
    draft = (
        '# Review\n\nSmith reports that "the effect held after controlling for age" (p. 2), '
        f"and that {LDQ}sleep cures everything{RDQ}.\n\n"
        "## References\n\n"
        "- Doe, J. (2021). Sleep and memory. Journal of Sleep, 3, 1-9.\n"
        "- Roe, A. (2019). A study we never read. https://doi.org/10.1234/abcd.5678\n"
        "- Poe, E. (2018). Ravens and memory.\n"
    )
    with Research.open(paths) as r:
        report = check_draft(r, "thesis", draft)
    assert [q.status for q in report.quotes] == ["elsewhere", "not_found"]
    # found although "con-" / "trolling" were split across lines; page 2 is in the range
    assert report.quotes[0].where in ("Sleep and Memory, p. 2", "Sleep and Memory, p. 1-2")
    assert [x.status for x in report.references] == ["in_library", "doi_unverified", "not_found"]
    assert report.problems == 2
    f = tmp_path / "draft.md"
    f.write_text(draft)
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    assert main(["--config-dir", str(paths.config_dir), "library", "check", "thesis", str(f)]) == 1
    out = capsys.readouterr().out
    assert "[BAD] quote" in out and "[doi] reference" in out and "2 problem(s)" in out
