from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from release.materialize_sdk import materialize

ROOT = Path(__file__).resolve().parents[1]
SDK_REF = "88ecbf8ac0b9a5c53665331322ccfb31b80458ab"
PYTHON_IMAGE = (
    "python:3.13-alpine@sha256:"
    "399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0"
)
PYTHON_SLIM_IMAGE = (
    "python:3.13-slim@sha256:"
    "6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
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
    assert "SOURCE_DIR: ares-source" in ci
    assert 'path: "${{ env.SOURCE_DIR }}"' in ci
    assert "Verify self-hosted build interpreter" in ci
    assert 'python3 "${SOURCE_DIR}/release/materialize_sdk.py"' in ci
    assert ci.count("actions/setup-python@") == 1
    assert "${{ github.event_name }}-${{ github.ref }}" in ci


CANARY_PATH = ROOT / ".github/workflows/sdk-canary.yml"
RUN_SCOPED_CHECKOUT = "sdk-canary-${{ github.run_id }}-${{ github.run_attempt }}"


def _canary() -> tuple[str, dict[str, Any]]:
    text = CANARY_PATH.read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def test_sdk_canary_checkout_is_scoped_to_the_run() -> None:
    """Fixní cesta `ares` zůstala na self-hosted runneru rozbitá a shodila
    checkout každého dalšího běhu (`git` exit 128) — cesta musí být unikátní
    pro `run_id` + `run_attempt` a canary musí běžet z ní."""

    text, workflow = _canary()
    assert workflow["env"]["CHECKOUT_DIR"] == RUN_SCOPED_CHECKOUT
    assert workflow["concurrency"] == {
        "group": "sdk-canary-${{ github.repository }}",
        "cancel-in-progress": False,
    }

    steps = workflow["jobs"]["canary"]["steps"]
    checkout, run_step = steps[0], steps[1]
    assert checkout["with"]["path"] == "${{ env.CHECKOUT_DIR }}"
    assert checkout["with"]["persist-credentials"] is False
    assert run_step["working-directory"] == "${{ env.CHECKOUT_DIR }}"
    assert run_step["continue-on-error"] is True

    # Cesty uvnitř kontejnerů jsou relativní k checkoutu, ne k `ares/`.
    assert "release/materialize_sdk.py --root . --output" in run_step["run"]
    assert "python -m pytest tests -q" in run_step["run"]
    assert PYTHON_SLIM_IMAGE in run_step["run"]
    assert "${SLUG}/" not in run_step["run"]

    # Hlášení issue zůstává zachované včetně oprávnění.
    assert workflow["permissions"] == {"contents": "read", "issues": "write"}
    assert steps[2]["if"] == "steps.run.outcome == 'failure'"
    assert "issues.create" in text and "labels: ['sdk-canary']" in text


def test_sdk_canary_cleanup_is_narrow_and_runs_after_checkout_post() -> None:
    """Úklid je samostatný job (běží až po „Post checkout"), maže výhradně dvě
    cesty pojmenované tímto během a nesmí použít `rm -rf` ani sáhnout na
    historický fixní adresář `ares`."""

    text, workflow = _canary()
    cleanup = workflow["jobs"]["cleanup"]
    assert cleanup["needs"] == "canary"
    assert cleanup["if"] == "always()"
    assert cleanup["permissions"] == {}

    script = cleanup["steps"][0]["run"]
    assert "docker run --rm -i" in script
    assert '-v "$GITHUB_WORKSPACE":/ws' in script
    assert '-v "$RUNNER_TEMP":/runner-temp' in script
    assert PYTHON_SLIM_IMAGE in script
    assert 'f"sdk-canary-{run_id}-{attempt}"' in script
    assert 'os.environ["CHECKOUT_DIR"] != expected_checkout' in script
    assert 'f"openmcp-sdk-{run_id}-{attempt}"' in script
    assert "root.is_absolute()" in script
    assert "root.is_symlink()" in script
    assert "target.parent != root" in script
    assert "target.is_symlink() or target.is_file()" in script
    assert "target.unlink()" in script
    assert script.count("shutil.rmtree(target)") == 1
    assert "os.path.lexists(target)" in script
    assert "except OSError" not in script
    assert "::warning::" not in script

    assert "rm -rf" not in text
    assert 'path: "ares"' not in text
    assert "/ares" not in script and '"ares"' not in script


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
