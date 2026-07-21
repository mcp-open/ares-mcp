"""Manifestové invarianty špecifické pre ARES.

Všeobecné kontroly (verzia manifest ↔ pyproject, `supports_test` wiring,
`display.tools` ↔ zaregistrované nástroje, slug proti Go validátoru) sú
v `test_conformance.py` — prišli zo SDK a boli doteraz rozkopírované po
konektoroch.
"""

from __future__ import annotations

from pathlib import Path

from openmcp_sdk.manifest import load_manifest

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "connector.yaml"


def test_is_a_no_secret_connector() -> None:
    """ARES je referenčný no-secret konektor — nič z toho nesmie pribudnúť.

    Prázdne `credentials` aj `user_config` sú to, čo v SDK vypína `_needs_vault`:
    hosted režim potom Vault vôbec nekontaktuje a nevyžaduje `VAULT_ADDR`.
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
    # `supports_test` je zámerne false — `/test` overuje credentials
    # konkrétneho používateľa a ARES žiadne nemá.
    assert manifest.capabilities.supports_test is False


def test_does_not_request_pii_salt() -> None:
    """ARES osobné údaje nepseudonymizuje — nesmie si teda pýtať salt.

    Mená štatutárov sú verejný údaj registra a vracajú sa s výslovným
    upozornením; dátum narodenia a bydlisko sa do LLM neprenášajú vôbec.
    Salt bez politiky by bol zbytočný k8s secret a `run_connector` by ho
    odmietol.
    """
    manifest = load_manifest(str(MANIFEST_PATH))
    assert manifest.runtime.pii_salt is False


def test_egress_points_at_ares() -> None:
    manifest = load_manifest(str(MANIFEST_PATH))
    assert manifest.egress is not None
    assert manifest.egress.host == "ares.gov.cz"
    assert manifest.egress.port == 443
