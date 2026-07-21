"""Negativní schema testy pro `connector.server.lookup_subjekt` (R1-WP06 brief,
úkol „Negative schema testy tests/test_schema.py").

Testují `lookup_subjekt` přímo (ne přes běžící MCP transport) — business
logika je záměrně oddělená od `@mcp.tool` dekorátoru přesně kvůli této
testovatelnosti (viz `server.py` docstring `lookup_subjekt`).

HTTP se od migrace na `openmcp_sdk.http.UpstreamClient` nemockuje přes
`httpx.get`/`httpx.post` (server už tyto funkce nevolá), ale přes injektovaný
`transport` — přesně k tomu `UpstreamClient` tuhle možnost má.
"""

from __future__ import annotations

import json as _json

import httpx
import pytest
from openmcp_sdk.envelope import ErrorCode
from openmcp_sdk.http import RetryPolicy, UpstreamClient

from connector import server

VALID_ICO = "27074358"  # skutečné IČO (Asseco Central Europe, a.s.), platný kontrolní součet


def _install(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Nahraď sdílený `server._client` klientem s mock transportem.

    `handler(request: httpx.Request) -> httpx.Response` — stejný kontrakt jako
    `httpx.MockTransport`. Retry politika kopíruje produkční nastavení
    (`RetryPolicy(max_attempts=1)`), aby testy „bez retry" zůstaly platné.
    """
    monkeypatch.setattr(
        server,
        "_client",
        UpstreamClient(
            base_url=server._ARES_REST,
            transport=httpx.MockTransport(handler),
            retry=RetryPolicy(max_attempts=1),
        ),
    )


def _forbidden(monkeypatch: pytest.MonkeyPatch, message: str = "upstream se neměl volat") -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(message)

    _install(monkeypatch, handler)


def _fixed(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    _install(monkeypatch, lambda request: response)


def test_invalid_format_short_ico_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """(1) ico='123' → invalid_input bez upstream callu."""
    _forbidden(monkeypatch, "upstream se neměl volat pro neplatný tvar IČO")

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt("123")

    assert exc_info.value.code is ErrorCode.INVALID_INPUT


def test_invalid_checksum_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """(2) ico='12345678' (8 číslic, neplatný kontrolní součet) → invalid_input."""
    assert server.ICO_RE.fullmatch("12345678")
    assert not server.ico_checksum("12345678")

    _forbidden(monkeypatch, "upstream se neměl volat pro neplatný kontrolní součet")

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt("12345678")

    assert exc_info.value.code is ErrorCode.INVALID_INPUT


def test_upstream_500_je_upstream_error_bez_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """(3) upstream 500 → typed upstream_error, bounded retry=0 (jediné volání)."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500, text="internal error")

    _install(monkeypatch, handler)

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt(VALID_ICO)

    assert exc_info.value.code is ErrorCode.UPSTREAM_ERROR
    assert len(calls) == 1  # bounded retry=0 — přesně jedno upstream volání


def test_upstream_timeout_je_upstream_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """(4) upstream timeout → upstream_unavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("connect timeout")

    _install(monkeypatch, handler)

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt(VALID_ICO)

    assert exc_info.value.code is ErrorCode.UPSTREAM_UNAVAILABLE


def test_response_nevalidna_proti_schema_je_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """(5) response neplatná proti outputSchema → internal, nikdy neplatný structuredContent.

    Simuluje ARES odpověď bez povinných polí (`obchodniJmeno`, `sidlo`) —
    `SubjektData(**payload)` selže na Pydantic validaci a connector to musí
    zachytit jako `internal`, nenechat projít syrovou `ValidationError` ani
    vrátit částečně vyplněný/neplatný výsledek.
    """
    _fixed(monkeypatch, httpx.Response(200, json={"ico": VALID_ICO}))

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt(VALID_ICO)

    assert exc_info.value.code is ErrorCode.INTERNAL


def test_non_object_json_body_je_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """(C16 bug scan) 200 s ne-objektovým JSON tělem (pole/scalar) → internal,
    ne neošetřený TypeError z `SubjektData(**payload)`."""
    for body in (["nope"], "text", 42):
        _fixed(monkeypatch, httpx.Response(200, json=body))

        with pytest.raises(server.ConnectorError) as exc_info:
            server.lookup_subjekt(VALID_ICO)

        assert exc_info.value.code is ErrorCode.INTERNAL


def test_non_json_body_je_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 s tělem, které není JSON (HTML chybovka z proxy) → internal.

    Předtím `_json_dict` nechal `JSONDecodeError` proletět a zachytila ji až
    `except (ValueError, ...)` u volajícího — tedy náhodou přes dědičnost.
    """
    _fixed(
        monkeypatch,
        httpx.Response(
            200,
            content=b"<html><body>502 Bad Gateway</body></html>",
            headers={"content-type": "text/html"},
        ),
    )

    with pytest.raises(server.ConnectorError) as exc_info:
        server.lookup_subjekt(VALID_ICO)

    assert exc_info.value.code is ErrorCode.INTERNAL


def test_valid_ico_checksum_pozitivny_kontrolny_pripad() -> None:
    """Kontrolní součet skutečného IČO je platný (pozitivní sanity check pro
    `ico_checksum` mimo pěti povinných negativních případů)."""
    assert server.ICO_RE.fullmatch(VALID_ICO)
    assert server.ico_checksum(VALID_ICO)


# --- ares_subjekt_vyhledat (search) ---------------------------------------


def test_search_kratke_jmeno_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jméno < 2 znaky → invalid_input bez POST na upstream."""
    _forbidden(monkeypatch, "upstream se neměl volat pro krátké jméno")
    with pytest.raises(server.ConnectorError) as exc:
        server.search_subjekt("A")
    assert exc.value.code is ErrorCode.INVALID_INPUT


def test_search_pocet_mimo_rozsah_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """pocet mimo 1..MAX_POCET → invalid_input bez upstreamu."""
    _forbidden(monkeypatch, "neměl se volat")
    with pytest.raises(server.ConnectorError) as exc:
        server.search_subjekt("Alza", pocet=server.MAX_POCET + 1)
    assert exc.value.code is ErrorCode.INVALID_INPUT


def test_search_happy_path_mapuje_polozky_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Položka s `ico` i položka bez `ico` (jen icoId) se namapují; celkem >
    vráceno → warning o oříznutí."""
    payload = {
        "pocetCelkem": 3,
        "ekonomickeSubjekty": [
            {
                "ico": "27074358",
                "obchodniJmeno": "Asseco Central Europe, a.s.",
                "pravniForma": "121",
                "sidlo": {"nazevObce": "Praha", "psc": 14000},
            },
            {
                "icoId": "ARES_00363445",
                "obchodniJmeno": "Asseco CE Cloud, a.s.",
                "pravniForma": "421",
                "sidlo": {"kodStatu": "SK", "pscTxt": "82104"},
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert _json.loads(request.content)["obchodniJmeno"] == "Asseco"
        return httpx.Response(200, json=payload)

    _install(monkeypatch, handler)
    res = server.search_subjekt("Asseco", pocet=2)
    assert res.data.pocet_celkem == 3
    assert [s.ico for s in res.data.subjekty] == ["27074358", None]
    assert res.warnings and "Nalezeno 3" in res.warnings[0]


def test_search_prazdny_vysledek_warning_ne_chyba(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 výsledků → validní envelope s prázdným seznamem + warning (ne chyba)."""
    _fixed(monkeypatch, httpx.Response(200, json={"pocetCelkem": 0, "ekonomickeSubjekty": []}))
    res = server.search_subjekt("Neexistujici Firma XYZ")
    assert res.data.subjekty == []
    assert res.warnings and "Žádný" in res.warnings[0]


def test_search_400_je_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARES 400 (neplatný filtr) → invalid_input."""
    _fixed(monkeypatch, httpx.Response(400, text="bad"))
    with pytest.raises(server.ConnectorError) as exc:
        server.search_subjekt("Alza")
    assert exc.value.code is ErrorCode.INVALID_INPUT


# --- ares_subjekt_vr (veřejný rejstřík, PII minimalizace) ------------------


def test_vr_invalid_ico_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neplatné IČO → invalid_input bez GET na upstream."""
    _forbidden(monkeypatch, "upstream se neměl volat pro neplatné IČO")
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_vr("123")
    assert exc.value.code is ErrorCode.INVALID_INPUT


def test_vr_prazdne_zaznamy_je_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subjekt bez záznamu ve VR (např. OSVČ) → invalid_input, ne internal."""
    _fixed(monkeypatch, httpx.Response(200, json={"icoId": VALID_ICO, "zaznamy": []}))
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_vr(VALID_ICO)
    assert exc.value.code is ErrorCode.INVALID_INPUT


_VR_ZAZNAM = {
    # Identitní pole jsou ve VR temporální historie, ne skaláry — aktuální
    # hodnota je záznam bez `datumVymazu`.
    "ico": [{"datumZapisu": "2003-08-06", "hodnota": VALID_ICO}],
    "obchodniJmeno": [
        {"datumZapisu": "2003-08-06", "datumVymazu": "2004-03-01", "hodnota": "Staré Jméno, a.s."},
        {"datumZapisu": "2004-03-01", "hodnota": "Asseco Central Europe, a.s."},
    ],
    "pravniForma": [{"datumZapisu": "2003-08-06", "hodnota": "121"}],
    "spisovaZnacka": [{"datumZapisu": "2003-08-06", "soud": "MSPH", "oddil": "B", "vlozka": 8525}],
    "statutarniOrgany": [
        {
            "nazevOrganu": "představenstvo",
            "clenoveOrganu": [
                {
                    "clenstvi": {"funkce": {"nazev": "člen představenstva"}},
                    "fyzickaOsoba": {
                        "jmeno": "MAREK",
                        "prijmeni": "GRÁC",
                        "datumNarozeni": "1972-01-14",
                        "adresa": {"textovaAdresa": "Stredná 27, Bratislava"},
                    },
                },
                {  # bývalý člen — má datumVymazu, musí být vynechán
                    "datumVymazu": "2020-10-16",
                    "fyzickaOsoba": {
                        "jmeno": "BÝVALÝ",
                        "prijmeni": "ČLEN",
                        "datumNarozeni": "1960-01-01",
                    },
                },
            ],
        },
        {  # zrušený orgán — celý vynechán
            "datumVymazu": "2019-01-01",
            "nazevOrganu": "starý orgán",
            "clenoveOrganu": [{"fyzickaOsoba": {"jmeno": "X", "prijmeni": "Y"}}],
        },
    ],
    "cinnosti": {
        "predmetPodnikani": [
            {"hodnota": "Činnost účetních poradců"},
            {"datumVymazu": "2009-05-04", "hodnota": "Ubytovací služby"},
        ]
    },
}


def test_vr_reduce_filtruje_a_neuniknou_pii() -> None:
    """`_reduce_vr`: jen aktuální člen + funkce; bývalý člen/orgán a zrušený
    předmět vynechány; datum narození ani adresa NEuniknou do výstupu."""
    data = server._reduce_vr(_VR_ZAZNAM)
    # aktuální hodnota z temporálních polí (ne stará/vymazaná)
    assert data.ico == VALID_ICO
    assert data.obchodni_jmeno == "Asseco Central Europe, a.s."
    assert data.spisova_znacka == "B 8525"
    assert [c.jmeno for c in data.statutarni_organ] == ["MAREK GRÁC"]
    assert data.statutarni_organ[0].funkce == "člen představenstva"
    assert data.statutarni_organ[0].organ == "představenstvo"
    assert data.predmet_podnikani == ["Činnost účetních poradců"]
    # PII nesmí být nikde v serializovaném výstupu
    blob = data.model_dump_json()
    assert "1972" not in blob and "Stredná" not in blob and "datumNarozeni" not in blob


def test_vr_happy_path_nese_pii_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kompletní VR volání vrací PII warning ve `warnings`."""
    _fixed(monkeypatch, httpx.Response(200, json={"icoId": VALID_ICO, "zaznamy": [_VR_ZAZNAM]}))
    res = server.lookup_vr(VALID_ICO)
    assert res.data.ico == VALID_ICO
    assert server.VR_PII_WARNING in res.warnings


# --- ares_subjekt_rzp (živnosti a provozovny) -----------------------------


def test_rzp_invalid_ico_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbidden(monkeypatch, "neměl se volat")
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_rzp("123")
    assert exc.value.code is ErrorCode.INVALID_INPUT


_RZP_ZAZNAM = {
    "ico": VALID_ICO,
    "obchodniJmeno": "Kaufland Česká republika v.o.s.",
    "pravniForma": "121",
    "zivnosti": [
        {
            "predmetPodnikani": "Výroba, obchod a služby",
            "druhZivnosti": "O",
            "provozovny": [
                {
                    "icp": 1001,
                    "nazev": "Kaufland Třeboň",
                    "typProvozovny": "1",
                    "sidloProvozovny": {"textovaAdresa": "Jiráskova 1315, Třeboň"},
                },
                {
                    "icp": 1002,
                    "nazev": "Kaufland Brno",
                    "platnostDo": "2020-01-01",
                    "sidloProvozovny": {"textovaAdresa": "Brno"},
                },  # zrušená
            ],
        },
        {
            "predmetPodnikani": "Hostinská činnost",
            "druhZivnosti": "R",
            # stejná provozovna jako u první živnosti → deduplikace dle icp
            "provozovny": [
                {
                    "icp": 1001,
                    "nazev": "Kaufland Třeboň",
                    "sidloProvozovny": {"textovaAdresa": "Jiráskova 1315, Třeboň"},
                }
            ],
        },
        {"predmetPodnikani": "Zaniklá živnost", "druhZivnosti": "V", "datumZaniku": "2015-01-01"},
    ],
}


def test_rzp_reduce_filtruje_a_deduplikuje() -> None:
    """Zaniklá živnost a zrušená provozovna vynechány; provozovna sdílená mezi
    živnostmi je v seznamu jen jednou (dedup dle icp)."""
    data = server._reduce_rzp(_RZP_ZAZNAM)
    assert [z.predmet for z in data.zivnosti] == ["Výroba, obchod a služby", "Hostinská činnost"]
    assert [p.nazev for p in data.provozovny] == ["Kaufland Třeboň"]
    assert data.provozovny[0].adresa == "Jiráskova 1315, Třeboň"


def test_rzp_prazdne_zaznamy_je_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch, httpx.Response(200, json={"icoId": VALID_ICO, "zaznamy": []}))
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_rzp(VALID_ICO)
    assert exc.value.code is ErrorCode.INVALID_INPUT


# --- ares_subjekt_res (NACE, kategorie počtu zaměstnanců) ------------------


def test_res_invalid_ico_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbidden(monkeypatch, "neměl se volat")
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_res("123")
    assert exc.value.code is ErrorCode.INVALID_INPUT


def test_res_happy_path_mapuje_nace_a_kategorii(monkeypatch: pytest.MonkeyPatch) -> None:
    zaznam = {
        "ico": VALID_ICO,
        "obchodniJmeno": "Asseco Central Europe, a.s.",
        "pravniForma": "121",
        "sidlo": {"nazevObce": "Praha", "psc": 14000, "textovaAdresa": "Praha 4"},
        "czNace": ["62010", 620],
        "statistickeUdaje": {
            "kategoriePoctuPracovniku": "330",
            "institucionalniSektor2010": "11003",
        },
    }
    _fixed(monkeypatch, httpx.Response(200, json={"icoId": VALID_ICO, "zaznamy": [zaznam]}))
    res = server.lookup_res(VALID_ICO)
    assert res.data.cz_nace == ["62010", "620"]
    assert res.data.kategorie_poctu_pracovniku == "330"
    assert res.data.sidlo is not None and res.data.sidlo.nazev_obce == "Praha"


# --- ares_adresa_standardizovat -------------------------------------------


def test_adresa_kratky_text_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbidden(monkeypatch, "neměl se volat")
    with pytest.raises(server.ConnectorError) as exc:
        server.standardizovat_adresu("Pr")
    assert exc.value.code is ErrorCode.INVALID_INPUT


def test_adresa_happy_path_posle_povinny_typ(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filtr musí obsahovat povinný typStandardizaceAdresy; položky se namapují."""
    payload = {
        "pocetCelkem": 1,
        "standardizovaneAdresy": [
            {
                "textovaAdresa": "Bucharova 2657/12, Praha 5",
                "nazevObce": "Praha",
                "nazevUlice": "Bucharova",
                "cisloDomovni": 2657,
                "psc": 15800,
                "kodAdresnihoMista": 27736342,
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        assert body["typStandardizaceAdresy"] == server.ADRESA_TYP_STANDARDIZACE
        return httpx.Response(200, json=payload)

    _install(monkeypatch, handler)
    res = server.standardizovat_adresu("Bucharova 2657 Praha", pocet=2)
    assert res.data.pocet_celkem == 1
    assert res.data.adresy[0].nazev_obce == "Praha"
    assert res.data.adresy[0].psc == 15800


# --- ares_subjekt_lookup: registrace + cz_nace (0.2.0) ---------------------


def test_lookup_odvodi_registrace_a_cz_nace(monkeypatch: pytest.MonkeyPatch) -> None:
    """`registrace` nese jen zdroje se stavem AKTIVNI (lowercase, seřazené);
    `cz_nace` se mapuje z `czNace`."""
    payload = {
        "ico": VALID_ICO,
        "obchodniJmeno": "Asseco Central Europe, a.s.",
        "pravniForma": "121",
        "sidlo": {"nazevObce": "Praha"},
        "czNace": ["62010", "620"],
        "seznamRegistraci": {
            "stavZdrojeVr": "AKTIVNI",
            "stavZdrojeRes": "AKTIVNI",
            "stavZdrojeDph": "AKTIVNI",
            "stavZdrojeRzp": "NEEXISTUJICI",
            "stavZdrojeCeu": "NEEXISTUJICI",
        },
    }
    _fixed(monkeypatch, httpx.Response(200, json=payload))
    res = server.lookup_subjekt(VALID_ICO)
    assert res.data.registrace == ["dph", "res", "vr"]
    assert res.data.cz_nace == ["62010", "620"]


def test_lookup_bez_seznamu_registraci_je_registrace_prazdna(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chybějící/ne-dict `seznamRegistraci` → prázdný seznam, ne chyba."""
    payload = {"ico": VALID_ICO, "obchodniJmeno": "X", "sidlo": {}}
    _fixed(monkeypatch, httpx.Response(200, json=payload))
    assert server.lookup_subjekt(VALID_ICO).data.registrace == []


# --- ares_subjekt_nrpzs (zdravotnická zařízení) ----------------------------


def test_nrpzs_invalid_ico_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbidden(monkeypatch, "neměl se volat")
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_nrpzs("123")
    assert exc.value.code is ErrorCode.INVALID_INPUT


_NRPZS_ZAZNAMY = [
    {
        "ico": VALID_ICO,
        "obchodniJmeno": "Fakultní nemocnice Motol",
        "pravniForma": "331",
        "druhZarizeni": "101",
        "primarniZaznam": True,
        "sidlo": {"textovaAdresa": "V úvalu 84/1, 15000 Praha 5"},
        "kontakty": {
            "telefon": "+420224431111",
            "email": "reditelstvi@fnmotol.cz",
            "www": "http://www.fnmotol.cz",
        },
        # PII — nesmí projít do výstupu
        "angazovaneOsoby": [{"jmeno": "JAN", "prijmeni": "ŘEDITEL", "datumNarozeni": "1970-01-01"}],
    },
    {
        "ico": VALID_ICO,
        "obchodniJmeno": "FN Motol — pracoviště 2",
        "druhZarizeni": "102",
        "sidlo": {"textovaAdresa": "Praha 5"},
    },
]


def test_nrpzs_reduce_nese_kontakty_a_neuniknou_pii() -> None:
    """Zařízení nesou institucionální kontakty; angažované osoby (jméno,
    datum narození) NEuniknou do serializovaného výstupu."""
    data = server._reduce_nrpzs(_NRPZS_ZAZNAMY)
    assert data.ico == VALID_ICO
    assert len(data.zarizeni) == 2
    assert data.zarizeni[0].telefon == "+420224431111"
    assert data.zarizeni[0].primarni is True
    assert data.zarizeni[1].druh_zarizeni == "102"
    blob = data.model_dump_json()
    assert "ŘEDITEL" not in blob and "1970" not in blob


def test_nrpzs_prazdne_zaznamy_je_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch, httpx.Response(200, json={"icoId": VALID_ICO, "zaznamy": []}))
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_nrpzs(VALID_ICO)
    assert exc.value.code is ErrorCode.INVALID_INPUT


# --- ares_ciselnik (překlad kódů) ------------------------------------------


def test_ciselnik_prazdny_kod_neni_volan_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbidden(monkeypatch, "neměl se volat")
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_ciselnik("  ")
    assert exc.value.code is ErrorCode.INVALID_INPUT


_CISELNIKY_PAYLOAD = {
    "pocetCelkem": 2,
    "ciselniky": [
        {
            "kodCiselniku": "PravniForma",
            "nazevCiselniku": "Pravní forma",
            "zdrojCiselniku": "res",
            "polozkyCiselniku": [
                {
                    "kod": "112",
                    "nazev": [
                        {"kodJazyka": "en", "nazev": "Limited company"},
                        {"kodJazyka": "cs", "nazev": "Společnost s ručením omezeným"},
                    ],
                },
                {"kod": "121", "nazev": [{"kodJazyka": "cs", "nazev": "Akciová společnost"}]},
                {"kod": "999", "nazev": []},
            ],
        },
        {"kodCiselniku": "PravniForma", "zdrojCiselniku": "com", "polozkyCiselniku": []},
    ],
}


def test_ciselnik_vybere_cesky_nazev_a_warning_o_zdrojich(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preferuje se český název (ne první jazyková mutace); více zdrojů →
    warning s výčtem zdrojů."""
    _fixed(monkeypatch, httpx.Response(200, json=_CISELNIKY_PAYLOAD))
    res = server.lookup_ciselnik("PravniForma")
    assert res.data.zdroj_ciselniku == "res"
    assert res.data.polozky[0].nazev == "Společnost s ručením omezeným"
    assert res.warnings and "více zdrojích" in res.warnings[0]


def test_ciselnik_filter_kod_vrati_jedinou_polozku(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch, httpx.Response(200, json=_CISELNIKY_PAYLOAD))
    res = server.lookup_ciselnik("PravniForma", kod="121")
    assert [p.kod for p in res.data.polozky] == ["121"]
    assert res.data.polozky[0].nazev == "Akciová společnost"


def test_ciselnik_filter_hledat_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch, httpx.Response(200, json=_CISELNIKY_PAYLOAD))
    res = server.lookup_ciselnik("PravniForma", hledat="akciová")
    assert [p.kod for p in res.data.polozky] == ["121"]


def test_ciselnik_orezanie_na_strop_s_warningom(monkeypatch: pytest.MonkeyPatch) -> None:
    """Více položek než MAX_CISELNIK_POLOZEK → oříznutí + warning."""
    velky = {
        "pocetCelkem": 1,
        "ciselniky": [
            {
                "kodCiselniku": "PravniForma",
                "zdrojCiselniku": "res",
                "polozkyCiselniku": [
                    {"kod": str(i), "nazev": [{"kodJazyka": "cs", "nazev": f"Forma {i}"}]}
                    for i in range(server.MAX_CISELNIK_POLOZEK + 10)
                ],
            }
        ],
    }
    _fixed(monkeypatch, httpx.Response(200, json=velky))
    res = server.lookup_ciselnik("PravniForma")
    assert len(res.data.polozky) == server.MAX_CISELNIK_POLOZEK
    assert res.data.pocet_celkem == server.MAX_CISELNIK_POLOZEK + 10
    assert any("vráceno prvních" in w for w in res.warnings)


def test_ciselnik_filter_prehlada_dalsie_zdroje(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kód, který v prvním zdroji není, se najde v dalším (com → res) —
    filtr nesmí skončit na prázdném prvním číselníku."""
    payload = {
        "pocetCelkem": 2,
        "ciselniky": [
            {
                "kodCiselniku": "PravniForma",
                "zdrojCiselniku": "com",
                "polozkyCiselniku": [
                    {"kod": "205", "nazev": [{"kodJazyka": "cs", "nazev": "Iná forma (com)"}]}
                ],
            },
            {
                "kodCiselniku": "PravniForma",
                "zdrojCiselniku": "res",
                "polozkyCiselniku": [
                    {
                        "kod": "112",
                        "nazev": [{"kodJazyka": "cs", "nazev": "Společnost s ručením omezeným"}],
                    }
                ],
            },
        ],
    }
    _fixed(monkeypatch, httpx.Response(200, json=payload))
    res = server.lookup_ciselnik("PravniForma", kod="112")
    assert res.data.zdroj_ciselniku == "res"
    assert [p.nazev for p in res.data.polozky] == ["Společnost s ručením omezeným"]


def test_ciselnik_nenalezen_je_invalid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch, httpx.Response(200, json={"pocetCelkem": 0, "ciselniky": []}))
    with pytest.raises(server.ConnectorError) as exc:
        server.lookup_ciselnik("NeexistujiciCiselnik")
    assert exc.value.code is ErrorCode.INVALID_INPUT
