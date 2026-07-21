"""Manifestové invarianty specifické pro ARES.

Obecné kontroly (verze manifest ↔ pyproject, `supports_test` wiring,
`display.tools` ↔ zaregistrované nástroje, slug proti Go validátoru) jsou
v `test_conformance.py` — přišly ze SDK a byly doposud rozkopírované po
konektorech.
"""

from __future__ import annotations

from pathlib import Path

from openmcp_sdk.manifest import load_manifest

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "connector.yaml"


def test_is_a_no_secret_connector() -> None:
    """ARES je referenční no-secret konektor — nic z toho nesmí přibýt.

    Prázdné `credentials` i `user_config` jsou to, co v SDK vypíná `_needs_vault`:
    hosted režim pak Vault vůbec nekontaktuje a nevyžaduje `VAULT_ADDR`.
    """
    manifest = load_manifest(str(MANIFEST_PATH))

    assert manifest.slug == "ares"
    assert manifest.credentials == []
    assert manifest.user_config == []
    assert manifest.operator_config == []
    assert manifest.auth.type == "credentials"


def test_is_read_only_over_public_registry() -> None:
    manifest = load_manifest(str(MANIFEST_PATH))

    assert manifest.capabilities.default_read_only is True
    assert manifest.capabilities.supports_write is False
    # `supports_test` je záměrně false — `/test` ověřuje credentials
    # konkrétního uživatele a ARES žádné nemá.
    assert manifest.capabilities.supports_test is False


def test_does_not_request_pii_salt() -> None:
    """ARES osobní údaje nepseudonymizuje — nesmí si tedy žádat salt.

    Jména statutárů jsou veřejný údaj registru a vracejí se s výslovným
    upozorněním; datum narození a bydliště se do LLM nepřenášejí vůbec.
    Salt bez politiky by byl zbytečný k8s secret a `run_connector` by ho
    odmítl.
    """
    manifest = load_manifest(str(MANIFEST_PATH))
    assert manifest.runtime.pii_salt is False


def test_egress_points_at_ares() -> None:
    manifest = load_manifest(str(MANIFEST_PATH))
    assert manifest.egress is not None
    assert manifest.egress.host == "ares.gov.cz"
    assert manifest.egress.port == 443
