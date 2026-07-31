"""Co uvidí MCP klient — plocha nástrojů a tvar chyby přes skutečný FastMCP.

`test_schema.py` volá business funkce přímo, takže neověří poslední krok:
jak FastMCP výsledek nebo výjimku předá klientovi. Právě tam se projevilo,
že neošetřená `AttributeError` odejde jako holý text Python výjimky **mimo**
typovanou obálku. Tenhle soubor proto jde přes in-memory `fastmcp.Client`.

Testy běží synchronně přes `asyncio.run` — repozitář nemá anyio/asyncio
pytest režim nastavený a kvůli dvěma testům ho zavádět nemusí.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastmcp import Client
from openmcp_sdk.http import RetryPolicy, UpstreamClient

from connector import server

VALID_ICO = "27074358"
CANARY = "KANAREK-mcp-9d2e4b-UPSTREAM-PII"


def _install(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    monkeypatch.setattr(
        server,
        "_client",
        UpstreamClient(
            base_url=server._ARES_REST,
            transport=httpx.MockTransport(lambda request: response),
            retry=RetryPolicy(max_attempts=1),
        ),
    )


async def _call(nazev: str, argumenty: dict[str, Any]) -> Any:
    async with Client(server.mcp) as client:
        return await client.call_tool(nazev, argumenty, raise_on_error=False)


async def _tools() -> list[Any]:
    async with Client(server.mcp) as client:
        return await client.list_tools()


def test_vsech_osm_nastroju_je_read_only() -> None:
    """`readOnlyHint` je bezpečnostní hranice: SDK fail-closed odregistruje
    každý nástroj bez ní, takže chybějící anotace znamená tiché zmizení."""
    nastroje = asyncio.run(_tools())

    assert len(nastroje) == 8
    assert {t.name for t in nastroje} == {
        "ares_adresa_standardizovat",
        "ares_ciselnik",
        "ares_subjekt_lookup",
        "ares_subjekt_nrpzs",
        "ares_subjekt_res",
        "ares_subjekt_rzp",
        "ares_subjekt_vr",
        "ares_subjekt_vyhledat",
    }
    assert all(t.annotations is not None for t in nastroje)
    assert all(t.annotations.readOnlyHint is True for t in nastroje)
    # `supports_write: false` v manifestu musí platit i na skutečné ploše.
    assert not [t for t in nastroje if t.annotations.readOnlyHint is not True]


def test_porusene_schema_dorazi_ke_klientovi_jako_typovana_obalka(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skalár místo objektu ve vnořeném poli → obálka s `error.code`.

    Dřív klient dostal `Error calling tool 'ares_subjekt_vr': 'str' object has
    no attribute 'get'` — bez kódu chyby a s interním detailem implementace.
    """
    _install(
        monkeypatch,
        httpx.Response(200, json={"zaznamy": [{"ico": VALID_ICO, "statutarniOrgany": [CANARY]}]}),
    )

    vysledek = asyncio.run(_call("ares_subjekt_vr", {"ico": VALID_ICO}))
    text = " ".join(str(getattr(c, "text", c)) for c in vysledek.content)

    assert vysledek.is_error
    assert '"code": "internal"' in text
    assert server.SCHEMA_ERROR_MSG in text
    assert CANARY not in text
    assert "object has no attribute" not in text


def test_vr_odpoved_nese_pii_varovani_a_zadne_rodne_udaje(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PII minimalizace platí i na výstupu skrze MCP, ne jen v redukční funkci."""
    _install(
        monkeypatch,
        httpx.Response(
            200,
            json={
                "zaznamy": [
                    {
                        "ico": [{"hodnota": VALID_ICO}],
                        "obchodniJmeno": [{"hodnota": "Asseco Central Europe, a.s."}],
                        "statutarniOrgany": [
                            {
                                "nazevOrganu": "představenstvo",
                                "clenoveOrganu": [
                                    {
                                        "clenstvi": {"funkce": {"nazev": "člen"}},
                                        "fyzickaOsoba": {
                                            "jmeno": "MAREK",
                                            "prijmeni": "GRÁC",
                                            "datumNarozeni": "1972-01-14",
                                            "adresa": {"textovaAdresa": "Stredná 27"},
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        ),
    )

    vysledek = asyncio.run(_call("ares_subjekt_vr", {"ico": VALID_ICO}))
    text = " ".join(str(getattr(c, "text", c)) for c in vysledek.content)

    assert not vysledek.is_error
    assert "MAREK GRÁC" in text  # jméno je účel nástroje a veřejný údaj
    assert server.VR_PII_WARNING in text
    assert "1972" not in text and "Stredná" not in text and "datumNarozeni" not in text
