"""ARES must use the shared SDK runtime even though it has no secrets."""

from pathlib import Path

from openmcp_sdk.runtime import run_connector


class _MCP:
    def list_tools_sync(self):
        return []


def test_no_secret_hosted_mode_does_not_require_vault() -> None:
    manifest = Path(__file__).parents[1] / "connector.yaml"
    identity, provider, transport = run_connector(
        str(manifest),
        _MCP(),
        argv_env={
            "OPENMCP_MODE": "hosted",
            "OPENMCP_INTERNAL_TOKEN": "x" * 32,
        },
        serve=False,
    )
    assert identity.principal_from_headers(
        {"x-openmcp-gateway-token": "x" * 32, "x-openmcp-sub": "u1"}
    ).sub == "u1"
    assert provider.resolve(
        identity.principal_from_headers(
            {"x-openmcp-gateway-token": "x" * 32, "x-openmcp-sub": "u1"}
        )
    ).secrets == {}
    assert transport == "http"
