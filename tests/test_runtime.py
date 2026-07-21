"""ARES musí používať zdieľaný SDK runtime, hoci nemá žiadne tajomstvá."""

from pathlib import Path

from openmcp_sdk.runtime import run_connector

from connector.server import mcp


def test_no_secret_hosted_mode_does_not_require_vault() -> None:
    """Hosted režim bez `credentials` nesmie vyžadovať Vault.

    Používa sa **reálna** `mcp` inštancia, nie stub s prázdnym zoznamom
    nástrojov: od SDK 0.4 `run_connector` overuje zhodu `display.tools` so
    zaregistrovanými nástrojmi, takže stub by test zhodil právom.

    Mutácia globálnej `mcp` tu nevadí — všetkých 8 nástrojov je read-only,
    takže read-only filter nemá čo odregistrovať. Pri konektore so
    zapisovacími nástrojmi by to muselo bežať v subprocese (robí to
    `ConnectorConformance`).
    """
    manifest = Path(__file__).parents[1] / "connector.yaml"
    identity, provider, transport = run_connector(
        str(manifest),
        mcp,
        argv_env={
            "OPENMCP_MODE": "hosted",
            "OPENMCP_INTERNAL_TOKEN": "x" * 32,
        },
        serve=False,
    )
    headers = {"x-openmcp-gateway-token": "x" * 32, "x-openmcp-sub": "u1"}
    assert identity.principal_from_headers(headers).sub == "u1"
    assert provider.resolve(identity.principal_from_headers(headers)).secrets == {}
    assert transport == "http"
