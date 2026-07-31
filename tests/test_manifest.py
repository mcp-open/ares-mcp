"""Manifestové invarianty specifické pro ARES.

Obecné kontroly (verze manifest ↔ pyproject, `supports_test` wiring,
`display.tools` ↔ zaregistrované nástroje, slug proti Go validátoru) jsou
v `test_conformance.py` — přišly ze SDK a byly doposud rozkopírované po
konektorech.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openmcp_sdk.envelope import ConnectorError, ErrorCode
from openmcp_sdk.identity.gateway import GatewayIdentity
from openmcp_sdk.manifest import load_manifest
from openmcp_sdk.runtime import _needs_vault
from openmcp_sdk.secrets.env import EnvProvider

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


def test_credential_owner_scope_je_neaplikovatelny() -> None:
    """Bez credentials nemá „vlastník přihlašovacích údajů" co vlastnit.

    ARES nemá `credentials` ani `user_config`, takže `_needs_vault` je False:
    hosted režim použije `EnvProvider`, `run_connector` nezapne
    `credential_version_test` a control plane pro tenhle konektor žádný
    owner nevydává. SDK to navíc **vynucuje** — `Principal` odmítne owner
    hlavičky bez `credential_version` jako neúplnou identitu, takže se sem
    owner scope nedá propašovat ani omylem v gateway konfiguraci.
    """
    manifest = load_manifest(str(MANIFEST_PATH))
    assert _needs_vault(manifest) is False

    identity = GatewayIdentity("x" * 32)
    hlavicky = {"x-openmcp-gateway-token": "x" * 32, "x-openmcp-sub": "u1"}

    principal = identity.principal_from_headers(hlavicky)
    assert principal.credential_version is None
    assert principal.credential_owner_kind is None
    assert principal.credential_owner_id is None
    assert EnvProvider(manifest, {}).resolve(principal).secrets == {}

    with pytest.raises(ConnectorError) as exc:
        identity.principal_from_headers(
            {
                **hlavicky,
                "x-openmcp-credential-owner-kind": "user",
                "x-openmcp-credential-owner-id": "u1",
            }
        )
    assert exc.value.code is ErrorCode.FORBIDDEN


def test_verejna_pii_je_deklarovana_a_minimalizovana() -> None:
    """Veřejná ≠ nechráněná.

    ARES vrací jména fyzických osob z veřejného rejstříku, takže se
    pseudonymizace (`runtime.pii_salt`) neuplatní — token by zničil užitečnost
    nástroje, jehož účelem je „kdo firmu zastupuje". Hranice je proto jinde:
    minimalizace na vstupu do modelu (`_reduce_vr` nese jen jméno a funkci),
    výslovné varování u odpovědi a katalogová deklarace v `display`.
    """
    from connector.server import VR_PII_WARNING

    manifest = load_manifest(str(MANIFEST_PATH))
    assert manifest.runtime.pii_salt is False
    assert "osobní údaj" in VR_PII_WARNING or "fyzických osob" in VR_PII_WARNING

    assert manifest.display is not None
    for text in (
        manifest.display.data_handling or "",
        (manifest.display.locales["cs"].data_handling or ""),
        (manifest.display.locales["sk"].data_handling or ""),
    ):
        assert text, "display.data_handling musí být vyplněné ve všech lokalizacích"
        assert "datum narození" in text or "dátum narodenia" in text


def test_egress_points_at_ares() -> None:
    manifest = load_manifest(str(MANIFEST_PATH))
    assert manifest.egress is not None
    assert manifest.egress.host == "ares.gov.cz"
    assert manifest.egress.port == 443
