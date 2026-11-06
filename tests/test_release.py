"""The release checks against a real build and a real SBOM, and rules for the workflows."""

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def load() -> ModuleType:
    script = ROOT / "scripts" / "release_check.py"
    spec = importlib.util.spec_from_file_location("release_check", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def uv() -> str:
    path = shutil.which("uv")
    if path is None:
        pytest.skip("uv not found")
    return path


def test_version_tag() -> None:
    rc = load()
    version = rc.project()["version"]
    assert rc.check_version(f"v{version}") is None
    assert "doesn't match" in rc.check_version("v99.0.0")
    assert "doesn't match" in rc.check_version(str(version))  # the v is required


def test_package_version_matches_pyproject() -> None:
    """`cairnward --version` prints __version__; a release is tagged from pyproject.toml. They must
    be the same, or a release reports the wrong number."""
    from cairnward import __version__

    assert __version__ == load().project()["version"]


def test_real_wheel_has_what_runs(tmp_path: Path) -> None:
    subprocess.run([uv(), "build", "--wheel", "--out-dir", str(tmp_path), "--project", str(ROOT)],
                   check=True, capture_output=True)  # fmt: skip
    rc = load()
    (wheel,) = tmp_path.glob("*.whl")
    assert rc.check_wheel(wheel) is None
    import zipfile

    empty = tmp_path / "empty.whl"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("cairnward/__init__.py", "")
    assert "is missing" in rc.check_wheel(empty)


def test_real_sbom_lists_runtime_dependencies_only(tmp_path: Path) -> None:
    out = tmp_path / "sbom.json"
    subprocess.run([uv(), "export", "--frozen", "--no-dev", "--format", "cyclonedx1.5", "-o",
                    str(out), "--project", str(ROOT)], check=True, capture_output=True)  # fmt: skip
    rc = load()
    assert rc.check_sbom(out) is None
    data = json.loads(out.read_text())
    data["components"] = [c for c in data["components"] if c["name"] != "pyrage"]
    out.write_text(json.dumps(data))
    assert "doesn't list pyrage" in rc.check_sbom(out)
    data["components"].append({"name": "pytest"})
    out.write_text(json.dumps(data))
    assert "development tools: pytest" in rc.check_sbom(out)
    out.write_text('{"bomFormat": "other"}')
    assert "isn't a CycloneDX" in rc.check_sbom(out)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_actions_are_pinned_to_commits(workflow: Path) -> None:
    uses = re.findall(r"uses:\s*(\S+)", workflow.read_text())
    assert uses
    for action in uses:
        assert re.search(r"@[0-9a-f]{40}$", action), action


def test_release_workflow_rules() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert re.search(r"tags: \[\"v\*\"\]", text) and "pull_request" not in text
    assert "--draft" in text  # a person publishes
    assert "pypi" not in text.lower()
    assert re.search(r"sigstore@\d+\.\d+\.\d+", text)  # a pinned version
    # no secret or untrusted tag name expanded directly inside a shell command
    for block in re.findall(r"run: \|?\n?(.*?)(?=\n\s+- |\Z)", text, re.S):
        assert "${{" not in block
    # the private-repository refusal comes before signing
    assert text.index("ALLOW_PUBLIC_SIGNING_LOG") < text.index("sigstore@")
