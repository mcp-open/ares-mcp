from __future__ import annotations

import re
from pathlib import Path

from release.materialize_sdk import materialize

ROOT = Path(__file__).resolve().parents[1]
SDK_REF = "eedc35a7de7ca61c6823d89a5048f9eff98e78ff"
PYTHON_IMAGE = (
    "python:3.13-alpine@sha256:"
    "399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0"
)


def test_reviewed_sdk_snapshot_materializes(tmp_path: Path) -> None:
    output = tmp_path / "sdk"
    assert materialize(ROOT, output) == SDK_REF
    assert (output / "pyproject.toml").is_file()
    assert (output / "openmcp_sdk" / "runtime.py").is_file()


def test_dependency_and_container_inputs_are_pinned() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert PYTHON_IMAGE in dockerfile
    assert (ROOT / ".sdk-ref").read_text(encoding="utf-8").strip() == SDK_REF
    assert "--require-hashes" in dockerfile
    assert "--no-deps --no-build-isolation" in dockerfile

    for relative in (
        "release/runtime-requirements.lock",
        "release/python-requirements.lock",
    ):
        lock = (ROOT / relative).read_text(encoding="utf-8")
        assert lock.count("--hash=sha256:") >= 70, relative


def test_ci_is_main_only_for_image_mutations() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "github.event_name == 'push'" in ci
    assert "github.ref == 'refs/heads/main'" in ci
    assert "release/materialize_sdk.py" in ci
    assert "repository: mcp-open/openmcp-sdk" not in ci
    assert "--require-hashes" in ci
    assert "--no-deps --no-build-isolation" in ci
    assert "trivy image --scanners vuln,secret" in ci


def test_workflow_actions_are_commit_pinned() -> None:
    workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))
    assert workflows
    for workflow_path in workflows:
        workflow = workflow_path.read_text(encoding="utf-8")
        for reference in re.findall(
            r"^\s*(?:-\s*)?uses:\s*([^\s#]+)",
            workflow,
            flags=re.MULTILINE,
        ):
            assert re.search(r"@[0-9a-f]{40}$", reference), (
                workflow_path,
                reference,
            )


def test_isolated_build_context_excludes_non_runtime_inputs() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for required in (
        "**/.github",
        "**/tests",
        "**/release/vendor",
        "**/release/evidence",
    ):
        assert required in dockerignore
