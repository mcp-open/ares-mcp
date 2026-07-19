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

from mcp_ares.schemas import (
    StatutarniClen,
    SubjektData,
    SubjektResult,
    SubjektSeznamData,
    SubjektSeznamResult,
    SubjektSummary,
    SubjektVrData,
    SubjektVrResult,
)

ARES_BASE_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty"
# Veřejný (obchodní) rejstřík — samostatný endpoint (statutární orgán, předmět
# podnikání), ktoré agregovaný `ekonomicke-subjekty/{ico}` nenesie.
ARES_VR_BASE_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty-vr"

# Vlastný strop veľkosti stránky vyhľadávania — chráni LLM kontext pred
# zahltením (ARES dovolí viac, ale desiatky subjektov v jednej odpovedi nemajú
# pre asistenta zmysel; `pocet_celkem` v odpovedi povie o orezaní).
MAX_POCET = 50

# Trvalé PII varovanie k VR výstupu — mená štatutárov sú verejný údaj registra,
# ale výslovne označíme, že odpoveď obsahuje osobné údaje fyzických osôb.
VR_PII_WARNING = (
    "Obsahuje jména fyzických osob z veřejného rejstříku. "
    "Datum narození a adresa bydliště byly záměrně vynechány."
)

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


def _get(url: str) -> httpx.Response:
    """Jedno GET upstream volanie — žiadny automatický retry (bounded retry=0)."""
    return httpx.get(url, timeout=BOUNDED_TIMEOUT)


def _post(url: str, body: dict) -> httpx.Response:
    """Jedno POST upstream volanie (vyhľadávanie) — bounded retry=0."""
    return httpx.post(url, json=body, timeout=BOUNDED_TIMEOUT)


def _raise_for_status(resp: httpx.Response, *, not_found_msg: str) -> None:
    """Spoločné mapovanie HTTP statusov na typované `ConnectorError`.

    Zhodné pre všetky tri nástroje: 429→rate_limited, 404→invalid_input
    (`not_found_msg`), iné 4xx→invalid_input (napr. ARES odmietne neplatný
    filter pri vyhľadávaní), 5xx→upstream_error. 2xx/3xx prejde bez chyby.
    """
    if resp.status_code == 429:
        raise ConnectorError(ErrorCode.RATE_LIMITED, "ARES vrátil 429 Too Many Requests")
    if resp.status_code == 404:
        raise ConnectorError(ErrorCode.INVALID_INPUT, not_found_msg)
    if 400 <= resp.status_code < 500:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, f"ARES odmítl dotaz (HTTP {resp.status_code})"
        )
    if resp.status_code >= 500:
        raise ConnectorError(
            ErrorCode.UPSTREAM_ERROR, f"ARES vrátil chybu HTTP {resp.status_code}"
        )


def _json_dict(resp: httpx.Response) -> dict:
    """`resp.json()` s poistkou na ne-objektové telo (C16 bug scan) — ARES môže
    pri chybe/proxy vrátiť 200 s poľom/skalárom; to je `internal`, nie
    neošetrený TypeError na `Model(**payload)`."""
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ConnectorError(
            ErrorCode.INTERNAL, "ARES vrátil neočekávaný tvar odpovědi (není objekt)"
        )
    return payload


def _call(do_request) -> httpx.Response:
    """Vykoná jedno upstream volanie a namapuje httpx zlyhania na typované
    `ConnectorError` (timeout/spojenie → upstream_unavailable). Bounded
    retry=0 — `do_request` sa volá práve raz."""
    try:
        return do_request()
    except httpx.TimeoutException as e:
        raise ConnectorError(
            ErrorCode.UPSTREAM_UNAVAILABLE, f"ARES neodpověděl v časovém limitu: {e}"
        ) from e
    except httpx.HTTPError as e:
        raise ConnectorError(ErrorCode.UPSTREAM_UNAVAILABLE, f"ARES je nedostupný: {e}") from e


def _provenance(resp: httpx.Response) -> Provenance:
    """Provenance pre live ARES odpoveď — `source_url` je skutočná volaná URL."""
    return Provenance(
        source_id="ares",
        source_url=str(resp.url),
        retrieved_at=now_utc_iso(),
        freshness="live",
    )


def lookup_subjekt(ico: str) -> SubjektResult:
    """Business logika `ares_subjekt_lookup`, oddelená od MCP dekorátora, aby
    ju negatívne schema testy (`tests/test_schema.py`) vedeli volať priamo bez
    bežiaceho MCP transportu."""
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    resp = _call(lambda: _get(f"{ARES_BASE_URL}/{ico}"))
    _raise_for_status(resp, not_found_msg=f"IČO {ico} nebylo v ARES nalezeno")

    payload = _json_dict(resp)
    try:
        data = SubjektData(**payload)
    except (ValueError, ValidationError, TypeError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    return SubjektResult(data=data, provenance=_provenance(resp), warnings=[])


def search_subjekt(
    obchodni_jmeno: str, adresa: str | None = None, start: int = 0, pocet: int = 10
) -> SubjektSeznamResult:
    """Business logika `ares_subjekt_vyhledat` (oddelená od MCP dekorátora kvôli
    testom). ARES vyžaduje `obchodniJmeno` ako primárny filter — samotná adresa
    či právna forma vráti 400, preto je meno povinné a adresa iba spresnenie."""
    jmeno = (obchodni_jmeno or "").strip()
    if len(jmeno) < 2:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "obchodní jméno musí mít alespoň 2 znaky"
        )
    if not 1 <= pocet <= MAX_POCET:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, f"pocet musí být v rozsahu 1..{MAX_POCET}"
        )
    if start < 0:
        raise ConnectorError(ErrorCode.INVALID_INPUT, "start nesmí být záporný")

    filtr: dict = {"obchodniJmeno": jmeno, "start": start, "pocet": pocet}
    adr = (adresa or "").strip()
    if adr:
        # `sidlo` je AdresaFiltr — z používateľsky zadateľných polí má iba
        # `textovaAdresa` (fulltext), nie nazevObce/psc.
        filtr["sidlo"] = {"textovaAdresa": adr}

    resp = _call(lambda: _post(f"{ARES_BASE_URL}/vyhledat", filtr))
    _raise_for_status(resp, not_found_msg="ARES vyhledávání nevrátilo výsledek")

    payload = _json_dict(resp)
    try:
        celkem = int(payload.get("pocetCelkem", 0))
        items = payload.get("ekonomickeSubjekty") or []
        subjekty = [SubjektSummary(**it) for it in items]
    except (ValueError, ValidationError, TypeError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    warnings: list[str] = []
    if celkem == 0:
        warnings.append("Žádný subjekt neodpovídá zadanému filtru.")
    elif celkem > len(subjekty):
        warnings.append(
            f"Nalezeno {celkem} subjektů, vráceno {len(subjekty)} "
            f"(upřesněte jméno/adresu nebo stránkujte přes 'start')."
        )

    return SubjektSeznamResult(
        data=SubjektSeznamData(
            pocet_celkem=celkem, start=start, pocet=pocet, subjekty=subjekty
        ),
        provenance=_provenance(resp),
        warnings=warnings,
    )


def lookup_vr(ico: str) -> SubjektVrResult:
    """Business logika `ares_subjekt_vr` — statutární orgán a předmět podnikání
    z Veřejného rejstříku. PII-minimalizované (viď `_reduce_vr`)."""
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    resp = _call(lambda: _get(f"{ARES_VR_BASE_URL}/{ico}"))
    _raise_for_status(resp, not_found_msg=f"IČO {ico} není ve veřejném rejstříku")

    payload = _json_dict(resp)
    try:
        zaznamy = payload.get("zaznamy") or []
        if not zaznamy:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT, f"IČO {ico} nemá záznam ve veřejném rejstříku"
            )
        data = _reduce_vr(zaznamy[0])
    except ConnectorError:
        raise
    except (ValueError, ValidationError, TypeError, KeyError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    return SubjektVrResult(data=data, provenance=_provenance(resp), warnings=[VR_PII_WARNING])


def _vr_aktualni(pole: object) -> object:
    """Z temporálneho VR poľa (list dated záznamov) vráti aktuálny (nevymazaný)
    prvok — VR nesie identitné polia (`ico`, `obchodniJmeno`, …) ako históriu
    zmien, nie skalár. Skalár/None vráti nezmenené (robustnosť)."""
    if not isinstance(pole, list):
        return pole
    aktualne = [x for x in pole if isinstance(x, dict) and not x.get("datumVymazu")]
    vyber = aktualne or pole
    return vyber[-1] if vyber else None


def _hodnota(x: object) -> str:
    """`.hodnota` z VR záznamu (alebo skalár/prázdno na string)."""
    if isinstance(x, dict):
        return str(x.get("hodnota") or "")
    return str(x) if x is not None else ""


def _reduce_vr(zaznam: dict) -> SubjektVrData:
    """Zredukuje jeden VR záznam na PII-minimalizovaný tvar.

    Filtruje iba **aktuálne** (nevymazané, bez `datumVymazu`) štatutárne orgány,
    ich členov a predmety podnikania. Z členov nesie iba meno + funkciu — datum
    narození ani adresu bydliska (`fyzickaOsoba.datumNarozeni`/`.adresa`) NIE.
    Právnická osoba ako člen sa nesie svojím obchodným menom. Identitné polia
    (`ico`/`obchodniJmeno`/…) nesie VR ako históriu — berieme aktuálnu hodnotu.
    """
    organy: list[StatutarniClen] = []
    for so in zaznam.get("statutarniOrgany") or []:
        if so.get("datumVymazu"):
            continue
        organ_nazev = so.get("nazevOrganu") or ""
        for clen in so.get("clenoveOrganu") or []:
            if clen.get("datumVymazu"):
                continue
            fo = clen.get("fyzickaOsoba") or {}
            jmeno = " ".join(p for p in (fo.get("jmeno"), fo.get("prijmeni")) if p).strip()
            if not jmeno:
                po = clen.get("pravnickaOsoba") or {}
                jmeno = (po.get("obchodniJmeno") or "").strip()
            if not jmeno:
                continue
            funkce = ((clen.get("clenstvi") or {}).get("funkce") or {}).get("nazev") or (
                clen.get("nazevAngazma") or ""
            )
            organy.append(StatutarniClen(jmeno=jmeno, funkce=funkce, organ=organ_nazev))

    predmety: list[str] = []
    for p in (zaznam.get("cinnosti") or {}).get("predmetPodnikani") or []:
        if p.get("datumVymazu"):
            continue
        hodnota = p.get("hodnota")
        if hodnota:
            predmety.append(hodnota)

    sz = _vr_aktualni(zaznam.get("spisovaZnacka"))
    if isinstance(sz, dict):
        spisova_znacka = " ".join(str(p) for p in (sz.get("oddil"), sz.get("vlozka")) if p)
    else:
        spisova_znacka = _hodnota(sz)

    return SubjektVrData(
        ico=_hodnota(_vr_aktualni(zaznam.get("ico"))),
        obchodni_jmeno=_hodnota(_vr_aktualni(zaznam.get("obchodniJmeno"))),
        pravni_forma=_hodnota(_vr_aktualni(zaznam.get("pravniForma"))),
        spisova_znacka=spisova_znacka,
        statutarni_organ=organy,
        predmet_podnikani=predmety,
    )


# Štruktúrované JSON logovanie (openmcp_sdk) — centrálny collector (Vector) ho
# rozbalí do poľa .app rovnako ako slog logy api/gateway. Component z env
# OPENMCP_COMPONENT (default mcp-ares); OPENMCP_LOG_FORMAT=text pre lokálny dev.
import os as _os  # noqa: E402
from openmcp_sdk.logging import setup as _log_setup  # noqa: E402

_log_setup(component=_os.getenv("OPENMCP_COMPONENT", "mcp-ares"))

mcp: FastMCP = FastMCP(
    "ares",
    instructions=(
        "Vyhledávání ekonomických subjektů v českém ARES. Veřejné API, bez přihlášení. "
        "Nástroje: ares_subjekt_lookup (detail podle IČO), ares_subjekt_vyhledat "
        "(hledání podle obchodního jména), ares_subjekt_vr (statutární orgán a předmět "
        "podnikání z veřejného rejstříku — obsahuje osobní údaje)."
    ),
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_subjekt_lookup(ico: str) -> SubjektResult:
    """Vyhledá ekonomický subjekt v ARES podle IČO (8 číslic)."""
    return lookup_subjekt(ico)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_subjekt_vyhledat(
    obchodni_jmeno: str, adresa: str | None = None, start: int = 0, pocet: int = 10
) -> SubjektSeznamResult:
    """Najde subjekty podle obchodního jména (když neznáš IČO).

    `obchodni_jmeno` je povinné (min. 2 znaky); `adresa` je volitelné fulltextové
    upřesnění sídla (např. město). `pocet` 1..50, `start` = offset pro stránkování.
    Vrací stránku výsledků + `pocet_celkem` (kolik jich ARES našel celkem).
    """
    return search_subjekt(obchodni_jmeno, adresa=adresa, start=start, pocet=pocet)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_subjekt_vr(ico: str) -> SubjektVrResult:
    """Statutární orgán a předmět podnikání subjektu z veřejného (obchodního) rejstříku.

    Vrací aktuální členy statutárního orgánu (jméno + funkce) a předmět podnikání.
    POZOR: obsahuje jména fyzických osob z veřejného rejstříku (datum narození a
    adresa bydliště jsou záměrně vynechány).
    """
    return lookup_vr(ico)


def main() -> None:
    # fastmcp 2.x: transport "http" == Streamable HTTP. host/port/path/
    # stateless_http sa v 2.x odovzdávajú do run() (nie do konštruktora) —
    # explicitne uvedené kvôli auditovateľnosti (gateway proxuje na :8000/mcp).
    mcp.run(transport="http", host="0.0.0.0", port=8000, path="/mcp", stateless_http=True)


if __name__ == "__main__":
    main()
