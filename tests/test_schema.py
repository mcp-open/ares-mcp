"""Negatívne schema testy pre `mcp_ares.server.lookup_subjekt` (R1-WP06 brief,
úloha „Negative schema testy tests/test_schema.py").

Testujú `lookup_subjekt` priamo (nie cez bežiaci MCP transport) — business
logika je zámerne oddelená od `@mcp.tool` dekorátora presne kvôli tejto
testovateľnosti (viď `server.py` docstring `lookup_subjekt`).
"""

from __future__ import annotations

import httpx
import pytest

from openmcp_sdk.envelope import ErrorCode
from mcp_ares import server

VALID_ICO = "27074358"  # skutočné IČO (Asseco Central Europe, a.s.), platný kontrolní součet


def test_invalid_format_short_ico_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """(1) ico='123' → invalid_input bez upstream callu."""
    called = {"n": 0}

    def fake_get(*args: object, **kwargs: object) -> httpx.Response:
        called["n"] += 1
        raise AssertionError("upstream sa nemal volať pre nevalidný tvar IČO")

    monkeypatch.setattr(server.httpx, "get", fake_get)

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt("123")

    assert exc_info.value.code is ErrorCode.INVALID_INPUT
    assert called["n"] == 0


def test_invalid_checksum_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """(2) ico='12345678' (8 číslic, neplatný kontrolní součet) → invalid_input."""
    assert server.ICO_RE.fullmatch("12345678")
    assert not server.ico_checksum("12345678")

    called = {"n": 0}

    def fake_get(*args: object, **kwargs: object) -> httpx.Response:
        called["n"] += 1
        raise AssertionError("upstream sa nemal volať pre neplatný kontrolný součet")

    monkeypatch.setattr(server.httpx, "get", fake_get)

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt("12345678")

    assert exc_info.value.code is ErrorCode.INVALID_INPUT
    assert called["n"] == 0


def test_upstream_500_je_upstream_error_bez_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """(3) upstream 500 → typed upstream_error, bounded retry=0 (jediné volanie)."""
    calls = []

    def fake_get(url: str, timeout: object) -> httpx.Response:
        calls.append(url)
        return httpx.Response(500, request=httpx.Request("GET", url), text="internal error")

    monkeypatch.setattr(server.httpx, "get", fake_get)

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt(VALID_ICO)

    assert exc_info.value.code is ErrorCode.UPSTREAM_ERROR
    assert len(calls) == 1  # bounded retry=0 — presne jedno upstream volanie


def test_upstream_timeout_je_upstream_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """(4) upstream timeout → upstream_unavailable."""

    def fake_get(url: str, timeout: object) -> httpx.Response:
        raise httpx.TimeoutException("connect timeout")

    monkeypatch.setattr(server.httpx, "get", fake_get)

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt(VALID_ICO)

    assert exc_info.value.code is ErrorCode.UPSTREAM_UNAVAILABLE


def test_response_nevalidna_proti_schema_je_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """(5) response nevalidná proti outputSchema → internal, nikdy nevalidný structuredContent.

    Simuluje ARES odpověď bez povinných polí (`obchodniJmeno`, `sidlo`) —
    `SubjektData(**payload)` selže na Pydantic validácii a connector to musí
    zachytiť ako `internal`, nie nechať prejsť surovú `ValidationError` ani
    vrátiť čiastočne vyplnený/nevalidný výsledok.
    """

    def fake_get(url: str, timeout: object) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url), json={"ico": VALID_ICO})

    monkeypatch.setattr(server.httpx, "get", fake_get)

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt(VALID_ICO)

    assert exc_info.value.code is ErrorCode.INTERNAL


def test_non_object_json_body_je_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """(C16 bug scan) 200 s ne-objektovým JSON telom (pole/scalar) → internal,
    nie neošetrený TypeError z `SubjektData(**payload)`."""
    for body in (["nope"], "text", 42):
        def fake_get(url: str, timeout: object, _b: object = body) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("GET", url), json=_b)

        monkeypatch.setattr(server.httpx, "get", fake_get)

        with pytest.raises(server.ConnectorError) as exc_info:
            server.lookup_subjekt(VALID_ICO)

        assert exc_info.value.code is ErrorCode.INTERNAL


def test_valid_ico_checksum_pozitivny_kontrolny_pripad() -> None:
    """Kontrolný súčet skutočného IČO je platný (pozitívny sanity check pre
    `ico_checksum` mimo piatich povinných negatívnych prípadov)."""
    assert server.ICO_RE.fullmatch(VALID_ICO)
    assert server.ico_checksum(VALID_ICO)
