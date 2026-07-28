"""ARES musí používat sdílený SDK runtime, i když nemá žádná tajemství."""

from pathlib import Path

from openmcp_sdk.runtime import run_connector

from connector.server import mcp, public_safe_test


def test_no_secret_hosted_mode_does_not_require_vault() -> None:
    """Hosted režim bez `credentials` nesmí vyžadovat Vault.

    Používá se **reálná** `mcp` instance, ne stub s prázdným seznamem
    nástrojů: od SDK 0.4 `run_connector` ověřuje shodu `display.tools` se
    zaregistrovanými nástroji, takže stub by test shodil právem.

    Mutace globální `mcp` tu nevadí — všech 8 nástrojů je read-only,
    takže read-only filtr nemá co odregistrovat. U konektoru se
    zapisovacími nástroji by to muselo běžet v subprocesu (dělá to
    `ConnectorConformance`).
    """
    manifest = Path(__file__).parents[1] / "connector.yaml"
    identity, provider, transport = run_connector(
        str(manifest),
        mcp,
        argv_env={
            "OPENMCP_MODE": "hosted",
            "OPENMCP_INTERNAL_TOKEN": "x" * 32,
            "OPENMCP_PUBLIC_SAFE_TEST_TOKEN": "p" * 32,
        },
        serve=False,
        public_safe_test=public_safe_test,
    )
    headers = {"x-openmcp-gateway-token": "x" * 32, "x-openmcp-sub": "u1"}
    assert identity.principal_from_headers(headers).sub == "u1"
    assert provider.resolve(identity.principal_from_headers(headers)).secrets == {}
    assert transport == "http"
