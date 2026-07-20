"""`connector.yaml` musí byť platný manifest podľa `openmcp_sdk.manifest`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from openmcp_sdk.manifest import load_manifest

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "connector.yaml"


def test_manifest_loads_and_validates() -> None:
    manifest = load_manifest(str(MANIFEST_PATH))

    assert manifest.slug == "ares"
    assert manifest.capabilities.default_read_only is True
    assert manifest.capabilities.supports_write is False

    # No-secret konektor: žiadne credentials ani používateľská konfigurácia.
    assert manifest.credentials == []
    assert manifest.user_config == []
    assert manifest.operator_config == []

    assert manifest.egress["host"] == "ares.gov.cz"
    assert manifest.egress["port"] == 443


def test_manifest_version_matches_package() -> None:
    """Verzia v manifeste je to, čo ukáže katalóg — nesmie zaostať za balíkom."""
    pyproject = (MANIFEST_PATH.parent / "pyproject.toml").read_text(encoding="utf-8")
    pkg_version = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in pyproject.splitlines()
        if line.startswith("version =")
    )
    manifest = load_manifest(str(MANIFEST_PATH))
    assert manifest.version == pkg_version


def test_supports_test_matches_runtime_wiring() -> None:
    """SDK invariant: `supports_test` musí sedieť s tým, či sa predáva `test_connection`.

    ARES ho zámerne nemá (no-secret konektor — niet čo overovať). Keby niekto
    pridal jedno bez druhého, `run_connector` spadne až za behu pri štarte.
    """
    manifest = load_manifest(str(MANIFEST_PATH))
    server_src = (MANIFEST_PATH.parent / "src" / "mcp_ares" / "server.py").read_text(
        encoding="utf-8"
    )
    passes_test_connection = "test_connection=" in server_src
    assert manifest.capabilities.supports_test == passes_test_connection


def test_display_tools_match_registered_tools() -> None:
    """`display.tools` je to, čo katalóg ukazuje — nesmie sa rozísť s realitou."""
    from mcp_ares import server

    raw = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    declared = {t["name"] for t in raw["display"]["tools"]}
    registered = set(asyncio.run(server.mcp.get_tools()))
    assert declared == registered
