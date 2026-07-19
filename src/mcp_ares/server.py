"""ARES no-secret referenčný connector (R1-WP06).

FastMCP Streamable HTTP server nad verejným ARES REST API
(`ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}`) — žiadny
credential, žiadny Vault, žiadny Broker (D-017 scope: no-secret connector).

Gateway (WP02, `platform/gateway/internal/proxy/proxy.go:UpstreamURL`)
proxuje na `http://mcp-ares.openmcp.svc.cluster.local:8000/mcp` — preto tento
proces musí počúvať na porte 8000 a ceste `/mcp` (FastMCP defaulty, explicitne
uvedené nižšie kvôli auditovateľnosti).
"""

from __future__ import annotations

import re

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from openmcp_sdk.envelope import ConnectorError, ErrorCode, Provenance, now_utc_iso

from mcp_ares.schemas import SubjektData, SubjektResult

ARES_BASE_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty"

# IČO = 8 číslic, kontrolný súčet podľa vyhlášky (modulo 11, váhy 8..2 na
# prvých 7 číslic). Bez tvarovej zhody sa upstream vôbec nevolá (J1 krok 3).
ICO_RE = re.compile(r"^\d{8}$")

# Krátky, ohraničený timeout — connector je interný bezstavový workload nad
# externým verejným API, nie dlhobežiaci proces (koncept: "bezpečnosť nie je
# demokratická", ale ohraničenosť čakania je hygiena, nie bezpečnostná otázka).
BOUNDED_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


def ico_checksum(ico: str) -> bool:
    """Overí kontrolný súčet 8-miestneho IČO (modulo 11, váhy 8..2).

    Predpokladá, že `ico` už prešlo `ICO_RE.fullmatch` (presne 8 číslic).
    """
    digits = [int(c) for c in ico]
    total = sum(d * w for d, w in zip(digits[:7], range(8, 1, -1)))
    remainder = total % 11
    if remainder in (0, 10):
        check = 1
    elif remainder == 1:
        check = 0
    else:
        check = 11 - remainder
    return check == digits[7]


def _fetch(ico: str) -> httpx.Response:
    """Jediné upstream volanie — žiadny automatický retry (bounded retry=0)."""
    return httpx.get(f"{ARES_BASE_URL}/{ico}", timeout=BOUNDED_TIMEOUT)


def lookup_subjekt(ico: str) -> SubjektResult:
    """Business logika `ares_subjekt_lookup`, oddelená od MCP dekorátora, aby
    ju negatívne schema testy (`tests/test_schema.py`) vedeli volať priamo bez
    bežiaceho MCP transportu."""
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    try:
        resp = _fetch(ico)
    except httpx.TimeoutException as e:
        raise ConnectorError(
            ErrorCode.UPSTREAM_UNAVAILABLE, f"ARES neodpověděl v časovém limitu: {e}"
        ) from e
    except httpx.HTTPError as e:
        raise ConnectorError(ErrorCode.UPSTREAM_UNAVAILABLE, f"ARES je nedostupný: {e}") from e

    if resp.status_code == 429:
        raise ConnectorError(ErrorCode.RATE_LIMITED, "ARES vrátil 429 Too Many Requests")
    if resp.status_code == 404:
        raise ConnectorError(ErrorCode.INVALID_INPUT, f"IČO {ico} nebylo v ARES nalezeno")
    if 400 <= resp.status_code < 500:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, f"ARES odmítl dotaz (HTTP {resp.status_code})"
        )
    if resp.status_code >= 500:
        raise ConnectorError(
            ErrorCode.UPSTREAM_ERROR, f"ARES vrátil chybu HTTP {resp.status_code}"
        )

    try:
        payload = resp.json()
        # C16 (bug scan): ARES môže (pri chybe/proxy) vrátiť 200 s ne-objektovým
        # JSON telom (pole, číslo, string). `SubjektData(**payload)` by na tom
        # vyhodilo neošetrený TypeError namiesto typovanej `internal` chyby.
        if not isinstance(payload, dict):
            raise ConnectorError(
                ErrorCode.INTERNAL, "ARES vrátil neočekávaný tvar odpovědi (není objekt)"
            )
        data = SubjektData(**payload)
    except (ValueError, ValidationError, TypeError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    return SubjektResult(
        data=data,
        provenance=Provenance(
            source_id="ares",
            source_url=str(resp.url),
            retrieved_at=now_utc_iso(),
            freshness="live",
        ),
        warnings=[],
    )


# Štruktúrované JSON logovanie (openmcp_sdk) — centrálny collector (Vector) ho
# rozbalí do poľa .app rovnako ako slog logy api/gateway. Component z env
# OPENMCP_COMPONENT (default mcp-ares); OPENMCP_LOG_FORMAT=text pre lokálny dev.
import os as _os  # noqa: E402
from openmcp_sdk.logging import setup as _log_setup  # noqa: E402

_log_setup(component=_os.getenv("OPENMCP_COMPONENT", "mcp-ares"))

mcp: FastMCP = FastMCP(
    "ares",
    instructions="Vyhledávání ekonomických subjektů v ARES podle IČO. Veřejné API, bez přihlášení.",
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_subjekt_lookup(ico: str) -> SubjektResult:
    """Vyhledá ekonomický subjekt v ARES podle IČO."""
    return lookup_subjekt(ico)


def main() -> None:
    # fastmcp 2.x: transport "http" == Streamable HTTP. host/port/path/
    # stateless_http sa v 2.x odovzdávajú do run() (nie do konštruktora) —
    # explicitne uvedené kvôli auditovateľnosti (gateway proxuje na :8000/mcp).
    mcp.run(transport="http", host="0.0.0.0", port=8000, path="/mcp", stateless_http=True)


if __name__ == "__main__":
    main()
