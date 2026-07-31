"""ARES no-secret referenční connector (R1-WP06).

FastMCP Streamable HTTP server nad veřejným ARES REST API
(`ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}`) — žádný
credential, žádný Vault, žádný Broker (D-017 scope: no-secret connector).

Gateway (WP02, `platform/gateway/internal/proxy/proxy.go:UpstreamURL`)
proxuje na `http://mcp-ares.openmcp.svc.cluster.local:8000/mcp` — proto tento
proces musí naslouchat na portu 8000 a cestě `/mcp` (FastMCP defaulty, explicitně
uvedené níže kvůli auditovatelnosti).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastmcp import FastMCP
from openmcp_sdk.envelope import ConnectorError, ErrorCode, Provenance, now_utc_iso
from openmcp_sdk.http import RetryPolicy, UpstreamClient
from openmcp_sdk.tools import tool
from pydantic import ValidationError

from connector.schemas import (
    AdresaItem,
    AdresaSeznamData,
    AdresaSeznamResult,
    CiselnikData,
    CiselnikPolozka,
    CiselnikResult,
    ProvozovnaItem,
    Sidlo,
    StatutarniClen,
    SubjektData,
    SubjektNrpzsData,
    SubjektNrpzsResult,
    SubjektResData,
    SubjektResResult,
    SubjektResult,
    SubjektRzpData,
    SubjektRzpResult,
    SubjektSeznamData,
    SubjektSeznamResult,
    SubjektSummary,
    SubjektVrData,
    SubjektVrResult,
    ZarizeniNrpzs,
    ZivnostItem,
)

logger = logging.getLogger(__name__)

_ARES_REST = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"
ARES_BASE_URL = "/ekonomicke-subjekty"
# Samostatné registry — nesou data, která agregovaný `ekonomicke-subjekty/{ico}`
# neobsahuje: VR (statutární orgán, předmět podnikání), RŽP (živnosti,
# provozovny), RES (NACE, kategorie počtu zaměstnanců).
ARES_VR_BASE_URL = "/ekonomicke-subjekty-vr"
ARES_RZP_BASE_URL = "/ekonomicke-subjekty-rzp"
ARES_RES_BASE_URL = "/ekonomicke-subjekty-res"
ARES_NRPZS_BASE_URL = "/ekonomicke-subjekty-nrpzs"
ARES_ADRESY_URL = "/standardizovane-adresy/vyhledat"
ARES_CISELNIKY_URL = "/ciselniky-nazevniky/vyhledat"

# `typStandardizaceAdresy` je povinný atribut filtru standardizace; ARES dovolí
# jen UPLNA_STANDARDIZACE | VYHOVUJICI_ADRESY — bereme úplnou standardizaci.
ADRESA_TYP_STANDARDIZACE = "UPLNA_STANDARDIZACE"
MAX_ADRES = 20

# Stropy pro číselníky a NRPZS zařízení — stejná motivace jako MAX_POCET:
# ochrana LLM kontextu (PravniForma má ~300 položek, nemocniční síť může mít
# desítky pracovišť; warning v odpovědi řekne o oříznutí).
MAX_CISELNIK_POLOZEK = 50
MAX_ZARIZENI = 50

# Stropy pro RŽP. Živnosti i provozovny přicházejí jako **vnořené** pole bez
# stránkovacího parametru — velikost odpovědi tedy neurčuje volající, ale
# subjekt: Česká pošta (IČO 47114983) má 1914 aktivních provozoven, ~136 kB
# JSONu v jediné odpovědi nástroje. Strop je proto na straně konektoru,
# stejně jako u NRPZS a číselníků, a `warnings` řekne skutečný počet.
MAX_ZIVNOSTI = 50
MAX_PROVOZOVEN = 50

# Strop pro ostatní vnořená výstupní pole, která volající nemá jak stránkovat
# (VR statutární orgán a předmět podnikání, RES seznam NACE). Na rozdíl od RŽP
# je u nich živý vzorek 313 subjektů nikdy nepřiblížil — maximum bylo 13
# statutárů, 26 předmětů a 23 NACE. Strop je tedy pojistka proti neohraničené
# upstream odpovědi, ne oříznutí reálných dat; proto je řádově vyšší.
MAX_VNORENYCH_POLOZEK = 200

# Stropy délky volného textu na vstupu. Bez nich může model poslat libovolně
# velký řetězec, který jde beze změny do těla POST požadavku na ARES —
# jediné volání nástroje uneslo 400 kB. Dolní meze (2/3 znaky) existovaly,
# horní ne.
MAX_TEXT_ZNAKU = 255
MAX_KOD_ZNAKU = 64

# Vlastní strop velikosti stránky vyhledávání — chrání LLM kontext před
# zahlcením (ARES dovolí víc, ale desítky subjektů v jedné odpovědi nemají
# pro asistenta smysl; `pocet_celkem` v odpovědi řekne o oříznutí).
MAX_POCET = 50

# Fixed public fixture for the control-plane readiness test. It is deliberately
# not configurable and never comes from a customer request.
PUBLIC_SAFE_TEST_ICO = "00006947"

# Trvalé PII varování k VR výstupu — jména statutárů jsou veřejný údaj rejstříku,
# ale výslovně označíme, že odpověď obsahuje osobní údaje fyzických osob.
VR_PII_WARNING = (
    "Obsahuje jména fyzických osob z veřejného rejstříku. "
    "Datum narození a adresa bydliště byly záměrně vynechány."
)

# IČO = 8 číslic, kontrolní součet podle vyhlášky (modulo 11, váhy 8..2 na
# prvních 7 číslic). Bez tvarové shody se upstream vůbec nevolá (J1 krok 3).
#
# `[0-9]`, ne `\d`: Pythonní `\d` matchuje i nearabské desítkové číslice
# (arabsko-indické ٠١٢…, devanágarí ०१२…). `int()` je přečte, takže takové
# „IČO" projde i kontrolním součtem, odejde percent-enkódované na ARES a
# vrátí se v chybové hlášce — přesně to volání navíc, kterému má validace
# předejít. IČO je z definice ASCII.
ICO_RE = re.compile(r"[0-9]{8}")

# Krátký, ohraničený timeout a bez retry — connector je interní bezstavový
# workload nad externím veřejným API, ne dlouhoběžící proces. `max_attempts=1`
# platí i pro GET (SDK default by na čtení zkoušel READ_RETRY) — ARES je
# veřejné API bez SLA a čekání na backoff nemá pro interaktivní dotaz smysl.
_client = UpstreamClient(
    base_url=_ARES_REST,
    timeout=5.0,
    connect_timeout=3.0,
    retry=RetryPolicy(max_attempts=1),
)


# Pevná hláška pro porušení schématu ARES odpovědi. Text výjimky se do ní
# NIKDY nevkládá — viz `_schema_error`.
SCHEMA_ERROR_MSG = "ARES vrátil odpověď, která neodpovídá očekávanému schématu"

# Warning, když `pocetCelkem` od ARES nejde použít — viz `_pocet_celkem`.
CELKEM_NEDUVERYHODNY_WARNING = (
    "ARES neuvedl použitelný celkový počet; 'pocet_celkem' proto odpovídá "
    "jen tomu, co je v této odpovědi vidět."
)


class ShapeError(Exception):
    """Prvek ARES odpovědi má jiný tvar, než kontrakt slibuje.

    Vlastní typ, protože se chytá spolu s ostatními porušeními schématu
    (`_SCHEMA_EXC`), ale na rozdíl od `AttributeError` z `"".get()` vzniká
    na kontrolovaném místě a nenese žádný obsah odpovědi.
    """


#: Čím se porušení kontraktu ARES odpovědi projeví. `AttributeError` a
#: `IndexError` jsou tu jako pojistka: dřív v seznamu chyběly, takže
#: `{"zaznamy": ["…"]}` shodilo nástroj syrovým `'str' object has no
#: attribute 'get'` mimo typovanou obálku.
_SCHEMA_EXC = (
    AttributeError,
    IndexError,
    KeyError,
    ShapeError,
    TypeError,
    ValidationError,
    ValueError,
)


def _schema_error(exc: Exception) -> ConnectorError:
    """Porušení schématu ARES jako typovaná chyba **bez** textu výjimky.

    `str(ValidationError)` obsahuje `input_value=…`, tedy doslovný výřez
    odpovědi ARES — u VR a NRPZS včetně osobních údajů, které tento konektor
    z výstupu záměrně odstraňuje. Do modelu proto jde pevná hláška a do logu
    jen jméno třídy výjimky, ne její text. Volající řetězí `raise … from
    None`, aby původní text neskončil ani v tracebacku v produkčním logu —
    stejný postup má `openmcp_sdk.http.UpstreamClient._json`.
    """
    logger.warning("odpověď ARES neodpovídá očekávanému schématu (%s)", type(exc).__name__)
    return ConnectorError(ErrorCode.INTERNAL, SCHEMA_ERROR_MSG)


def _map(value: object) -> dict[str, Any]:
    """Vnořený objekt z ARES odpovědi; chybějící nebo `null` → prázdný."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ShapeError("očekáván objekt")
    return value


def _maps(value: object) -> list[dict[str, Any]]:
    """Pole objektů z ARES odpovědi; chybějící nebo `null` → prázdné.

    Kontroluje i prvky. Bez toho stačí `{"zaznamy": ["x"]}` a redukční
    funkce spadne na `AttributeError` uvnitř `.get()`.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ShapeError("očekáváno pole")
    if any(not isinstance(item, dict) for item in value):
        raise ShapeError("očekáváno pole objektů")
    return value


def _texts(value: object) -> list[str]:
    """Pole skalárů z ARES odpovědi; chybějící nebo `null` → prázdné.

    Řetězec ani objekt **nejsou** pole, i když se přes ně dá iterovat:
    `for x in "62010"` se rozpadne na znaky a `for x in {"kod": …}` na klíče.
    Bez téhle kontroly vracel RES na `czNace: "62010"` tiše
    `['6', '2', '0', '1', '0']` — poškozená data místo chyby schématu.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ShapeError("očekáváno pole skalárů")
    if any(
        item is not None
        and (isinstance(item, bool) or not isinstance(item, (str, int, float)))
        for item in value
    ):
        raise ShapeError("očekáváno pole skalárů")
    return [t for t in (_text(x) for x in value) if t]


def _bool(value: object) -> bool:
    """Pravdivostní pole ARES odpovědi.

    `bool(value)` je nad cizími daty past: `bool("false")` i `bool("0")` jsou
    True, stejně jako `bool({...})`. Bereme proto jen skutečný JSON boolean
    a jeho textovou podobu; cokoli jiného je False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1"}
    return False


def _volny_text(hodnota: str | None, popis: str, minimum: int, maximum: int) -> str:
    """Ořež a ověř délku volného textu ze vstupu nástroje.

    Horní mez je stejně důležitá jako dolní: bez ní jde neomezený řetězec
    rovnou do těla POST požadavku na ARES.
    """
    text = (hodnota or "").strip()
    if len(text) < minimum:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, f"{popis} musí mít alespoň {minimum} znaky"
        )
    if len(text) > maximum:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, f"{popis} smí mít nejvýše {maximum} znaků"
        )
    return text


def _text(value: object) -> str:
    """Skalární pole ARES odpovědi jako text; objekt nebo pole → prázdno.

    `str(value)` by z objektu udělal jeho Python repr a propašoval tak celý
    (potenciálně osobní) podstrom do odpovědi pro model — nese-li ARES na
    místě skaláru objekt, je to porušení schématu, ne text.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return str(value)


def _pocet_celkem(raw: object, minimum: int) -> tuple[int, bool]:
    """Použitelný `pocet_celkem` a příznak, že ho ARES nedodal.

    `pocetCelkem` může chybět, přijít nečíselný, záporný nebo **menší** než
    počet položek v téže odpovědi. Tvrdit „nalezeno 0" nad neprázdným
    seznamem je lež, kterou model nemá jak odhalit; v takovém případě se
    vrací `minimum` (co je opravdu vidět) a odpověď to řekne ve `warnings`.
    """
    hodnota: int | None
    if isinstance(raw, bool):
        hodnota = None
    elif isinstance(raw, float):
        # `int(12.9)` je 12 — tichá ztráta zbytku. Neceločíselný počet je
        # nesmysl, takže se bere jako „ARES ho nedodal", ne jako 12.
        hodnota = int(raw) if raw.is_integer() else None
    elif isinstance(raw, (int, str)):
        try:
            hodnota = int(raw)
        except (TypeError, ValueError):
            hodnota = None
    else:
        hodnota = None
    if hodnota is None or hodnota < 0 or hodnota < minimum:
        return minimum, True
    return hodnota, False


def ico_checksum(ico: str) -> bool:
    """Ověří kontrolní součet 8místného IČO (modulo 11, váhy 8..2).

    Předpokládá, že `ico` už prošlo `ICO_RE.fullmatch` (přesně 8 číslic).
    """
    digits = [int(c) for c in ico]
    # strict=True: obě sekvence mají přesně 7 prvků, takže tichá neshoda by
    # byla chyba ve výpočtu kontrolního součtu, ne legitimní stav.
    total = sum(d * w for d, w in zip(digits[:7], range(8, 1, -1), strict=True))
    remainder = total % 11
    if remainder in (0, 10):
        check = 1
    elif remainder == 1:
        check = 0
    else:
        check = 11 - remainder
    return check == digits[7]


def _fetch(
    method: str, path: str, *, not_found_msg: str, body: dict[str, Any] | None = None
) -> tuple[dict[str, Any], httpx.Response]:
    """Upstream volání přes sdílený `_client` — retry, timeout a obecné mapování
    HTTP stavů (401/403/429/5xx) dělá `openmcp_sdk.http`.

    Co zůstává doménové a obecný klient to neumí: **konkrétní 404 zpráva**
    (`not_found_msg`, jiná pro každý registr) a vrácení `httpx.Response`, aby
    `_provenance` mohla použít skutečnou volanou URL.
    """
    try:
        resp = _client.request(method, path, json=body)
    except ConnectorError as exc:
        if exc.status == 404:
            raise ConnectorError(ErrorCode.INVALID_INPUT, not_found_msg) from exc
        raise
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ConnectorError(
            ErrorCode.INTERNAL, "ARES vrátil odpověď, která není platný JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ConnectorError(
            ErrorCode.INTERNAL, "ARES vrátil neočekávaný tvar odpovědi (není objekt)"
        )
    return payload, resp


def _provenance(resp: httpx.Response) -> Provenance:
    """Provenance pro live ARES odpověď — `source_url` je skutečná volaná URL."""
    return Provenance(
        source_id="ares",
        source_url=str(resp.url),
        retrieved_at=now_utc_iso(),
        freshness="live",
    )


# Logging nastavuje `run_connector` sám (od SDK 0.4) — component odvodí ze
# slugu v manifestu. Volat `logging.setup()` tu na úrovni importu je špatná
# vrstva: při importu modulu v testu nebo nástroji by přepsalo cizí
# konfiguraci.

mcp: FastMCP = FastMCP(
    "ares",
    instructions=(
        "Vyhledávání ekonomických subjektů v českém ARES. Veřejné API, bez přihlášení. "
        "Nástroje: ares_subjekt_lookup (detail podle IČO; pole 'registrace' říká, ve "
        "kterých registrech má subjekt aktivní záznam), ares_subjekt_vyhledat "
        "(hledání podle obchodního jména), ares_subjekt_vr (statutární orgán a předmět "
        "podnikání z veřejného rejstříku — obsahuje osobní údaje), ares_subjekt_rzp "
        "(živnosti a provozovny), ares_subjekt_res (NACE a kategorie počtu zaměstnanců), "
        "ares_subjekt_nrpzs (zdravotnická zařízení a jejich kontakty), "
        "ares_adresa_standardizovat (standardizace/našeptávač adresy podle RÚIAN), "
        "ares_ciselnik (překlad kódů z odpovědí — např. PravniForma 112 → název)."
    ),
)


@tool(mcp, read_only=True, name="ares_subjekt_lookup")
def lookup_subjekt(ico: str) -> SubjektResult:
    """Vyhledá ekonomický subjekt v ARES podle IČO (8 číslic).

    Vrací základní detail (jméno, sídlo, právní forma, DIČ, NACE) a pole
    `registrace` — seznam registrů s aktivním záznamem (vr → ares_subjekt_vr,
    rzp → ares_subjekt_rzp, res → ares_subjekt_res, nrpzs → ares_subjekt_nrpzs).
    """
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    payload, resp = _fetch(
        "GET", f"{ARES_BASE_URL}/{ico}", not_found_msg=f"IČO {ico} nebylo v ARES nalezeno"
    )
    try:
        data = SubjektData(**payload)
        data.registrace = _aktivni_registrace(payload.get("seznamRegistraci"))
    except _SCHEMA_EXC as e:
        raise _schema_error(e) from None

    return SubjektResult(data=data, provenance=_provenance(resp), warnings=[])


def public_safe_test() -> None:
    """Perform one real, bounded ARES lookup and discard all provider data."""

    result = lookup_subjekt(PUBLIC_SAFE_TEST_ICO)
    if (
        result.data.ico != PUBLIC_SAFE_TEST_ICO
        or not result.data.obchodni_jmeno.strip()
    ):
        raise ConnectorError(
            ErrorCode.INTERNAL,
            "syntetická kontrola ARES nevrátila očekávaný subjekt",
        )


def _aktivni_registrace(seznam: object) -> list[str]:
    """Ze `seznamRegistraci` (klíče `stavZdrojeXxx`) vrátí seznam registrů
    s hodnotou AKTIVNI jako lowercase zkratky (`vr`, `res`, `rzp`, `dph`, …) —
    LLM z nich vidí, který follow-up nástroj má smysl volat."""
    if not isinstance(seznam, dict):
        return []
    prefix = "stavZdroje"
    return sorted(
        k[len(prefix):].lower()
        for k, v in seznam.items()
        if k.startswith(prefix) and v == "AKTIVNI"
    )


@tool(mcp, read_only=True, name="ares_subjekt_vyhledat")
def search_subjekt(
    obchodni_jmeno: str, adresa: str | None = None, start: int = 0, pocet: int = 10
) -> SubjektSeznamResult:
    """Najde subjekty podle obchodního jména (když neznáš IČO).

    `obchodni_jmeno` je povinné (min. 2 znaky); `adresa` je volitelné fulltextové
    upřesnění sídla (např. město). `pocet` 1..50, `start` = offset pro stránkování.
    Vrací stránku výsledků + `pocet_celkem` (kolik jich ARES našel celkem).
    """
    # ARES vyžaduje `obchodniJmeno` jako primární filtr — samotná adresa či
    # právní forma vrátí 400. Proto je jméno povinné a adresa jen upřesnění.
    jmeno = _volny_text(obchodni_jmeno, "obchodní jméno", 2, MAX_TEXT_ZNAKU)
    adr = _volny_text(adresa, "adresa", 0, MAX_TEXT_ZNAKU)
    if not 1 <= pocet <= MAX_POCET:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, f"pocet musí být v rozsahu 1..{MAX_POCET}"
        )
    if start < 0:
        raise ConnectorError(ErrorCode.INVALID_INPUT, "start nesmí být záporný")

    filtr: dict[str, Any] = {"obchodniJmeno": jmeno, "start": start, "pocet": pocet}
    if adr:
        # `sidlo` je AdresaFiltr — z uživatelsky zadatelných polí má jen
        # `textovaAdresa` (fulltext), ne nazevObce/psc.
        filtr["sidlo"] = {"textovaAdresa": adr}

    payload, resp = _fetch(
        "POST",
        f"{ARES_BASE_URL}/vyhledat",
        body=filtr,
        not_found_msg="ARES vyhledávání nevrátilo výsledek",
    )
    try:
        subjekty = [SubjektSummary(**it) for it in _maps(payload.get("ekonomickeSubjekty"))]
    except _SCHEMA_EXC as e:
        raise _schema_error(e) from None

    warnings: list[str] = []
    # Strop se vynucuje lokálně, ne jen prosbou v `filtr["pocet"]`. Kdyby ho
    # ARES ignoroval, chrání LLM kontext jen tahle podmínka.
    if len(subjekty) > pocet:
        warnings.append(
            f"ARES vrátil {len(subjekty)} položek místo požadovaných {pocet}; "
            f"odpověď je oříznutá na {pocet}."
        )
        subjekty = subjekty[:pocet]

    vraceno = len(subjekty)
    # Dolní mez celkového počtu známe jen z neprázdné stránky: `start` sám
    # o sobě neříká, že tolik záznamů existuje (offset za koncem vrátí nic).
    celkem, dopocteno = _pocet_celkem(
        payload.get("pocetCelkem"), start + vraceno if vraceno else 0
    )
    if dopocteno:
        warnings.append(CELKEM_NEDUVERYHODNY_WARNING)
    if celkem == 0:
        warnings.append("Žádný subjekt neodpovídá zadanému filtru.")
    elif vraceno == 0:
        warnings.append(
            f"Stránka od pozice {start} je prázdná; ARES hlásí celkem {celkem} "
            f"subjektů — zkuste nižší 'start'."
        )
    elif start + vraceno < celkem:
        warnings.append(
            f"Nalezeno {celkem} subjektů, vráceno {vraceno} od pozice {start} "
            f"(upřesněte jméno/adresu nebo stránkujte přes 'start')."
        )

    return SubjektSeznamResult(
        data=SubjektSeznamData(
            pocet_celkem=celkem, start=start, pocet=pocet, subjekty=subjekty
        ),
        provenance=_provenance(resp),
        warnings=warnings,
    )


@tool(mcp, read_only=True, name="ares_subjekt_vr")
def lookup_vr(ico: str) -> SubjektVrResult:
    """Statutární orgán a předmět podnikání subjektu z veřejného (obchodního) rejstříku.

    Vrací aktuální členy statutárního orgánu (jméno + funkce) a předmět podnikání.
    POZOR: obsahuje jména fyzických osob z veřejného rejstříku (datum narození a
    adresa bydliště jsou záměrně vynechány).
    """
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    payload, resp = _fetch(
        "GET",
        f"{ARES_VR_BASE_URL}/{ico}",
        not_found_msg=f"IČO {ico} není ve veřejném rejstříku",
    )
    try:
        zaznamy = _maps(payload.get("zaznamy"))
        if not zaznamy:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT, f"IČO {ico} nemá záznam ve veřejném rejstříku"
            )
        data, orezani = _reduce_vr(zaznamy[0])
    except ConnectorError:
        raise
    except _SCHEMA_EXC as e:
        raise _schema_error(e) from None

    return SubjektVrResult(
        data=data, provenance=_provenance(resp), warnings=[VR_PII_WARNING, *orezani]
    )


def _vr_aktualni(pole: object) -> object:
    """Z temporálního VR pole (list dated záznamů) vrátí aktuální (nevymazaný)
    prvek — VR nese identitní pole (`ico`, `obchodniJmeno`, …) jako historii
    změn, ne skalár. Skalár/None vrátí nezměněné (robustnost)."""
    if not isinstance(pole, list):
        return pole
    aktualne = [x for x in pole if isinstance(x, dict) and not x.get("datumVymazu")]
    vyber = aktualne or pole
    return vyber[-1] if vyber else None


def _hodnota(x: object) -> str:
    """`.hodnota` z VR záznamu (nebo skalár/prázdno na string)."""
    if isinstance(x, dict):
        return _text(x.get("hodnota"))
    return _text(x)


def _reduce_vr(zaznam: dict[str, Any]) -> tuple[SubjektVrData, list[str]]:
    """Zredukuje jeden VR záznam na PII-minimalizovaný tvar.

    Filtruje jen **aktuální** (nevymazané, bez `datumVymazu`) statutární orgány,
    jejich členy a předměty podnikání. Z členů nese jen jméno + funkci — datum
    narození ani adresu bydliště (`fyzickaOsoba.datumNarozeni`/`.adresa`) NE.
    Právnická osoba jako člen se nese svým obchodním jménem. Identitní pole
    (`ico`/`obchodniJmeno`/…) nese VR jako historii — bereme aktuální hodnotu.

    Vrací i `warnings` k oříznutí: obě vnořená pole jdou přes
    `MAX_VNORENYCH_POLOZEK` (viz konstanta — pojistka, ne běžný stav).
    """
    organy: list[StatutarniClen] = []
    for so in _maps(zaznam.get("statutarniOrgany")):
        if so.get("datumVymazu"):
            continue
        organ_nazev = _text(so.get("nazevOrganu"))
        for clen in _maps(so.get("clenoveOrganu")):
            if clen.get("datumVymazu"):
                continue
            fo = _map(clen.get("fyzickaOsoba"))
            # Bereme VÝHRADNĚ jméno a příjmení; `datumNarozeni`, `adresa`
            # ani občanství se odsud nikdy nečtou (PII minimalizace).
            jmeno = " ".join(
                p for p in (_text(fo.get("jmeno")), _text(fo.get("prijmeni"))) if p
            ).strip()
            if not jmeno:
                po = _map(clen.get("pravnickaOsoba"))
                jmeno = _text(po.get("obchodniJmeno")).strip()
            if not jmeno:
                continue
            funkce = _text(_map(_map(clen.get("clenstvi")).get("funkce")).get("nazev")) or _text(
                clen.get("nazevAngazma")
            )
            organy.append(StatutarniClen(jmeno=jmeno, funkce=funkce, organ=organ_nazev))

    predmety: list[str] = []
    for p in _maps(_map(zaznam.get("cinnosti")).get("predmetPodnikani")):
        if p.get("datumVymazu"):
            continue
        hodnota = _text(p.get("hodnota"))
        if hodnota:
            predmety.append(hodnota)

    sz = _vr_aktualni(zaznam.get("spisovaZnacka"))
    if isinstance(sz, dict):
        spisova_znacka = " ".join(
            p for p in (_text(sz.get("oddil")), _text(sz.get("vlozka"))) if p
        )
    else:
        spisova_znacka = _hodnota(sz)

    warnings: list[str] = []
    if len(organy) > MAX_VNORENYCH_POLOZEK:
        warnings.append(
            f"Subjekt má {len(organy)} aktuálních členů statutárních orgánů, "
            f"vráceno prvních {MAX_VNORENYCH_POLOZEK}."
        )
    if len(predmety) > MAX_VNORENYCH_POLOZEK:
        warnings.append(
            f"Subjekt má {len(predmety)} aktuálních předmětů podnikání, "
            f"vráceno prvních {MAX_VNORENYCH_POLOZEK}."
        )

    return (
        SubjektVrData(
            ico=_hodnota(_vr_aktualni(zaznam.get("ico"))),
            obchodni_jmeno=_hodnota(_vr_aktualni(zaznam.get("obchodniJmeno"))),
            pravni_forma=_hodnota(_vr_aktualni(zaznam.get("pravniForma"))),
            spisova_znacka=spisova_znacka,
            statutarni_organ=organy[:MAX_VNORENYCH_POLOZEK],
            predmet_podnikani=predmety[:MAX_VNORENYCH_POLOZEK],
        ),
        warnings,
    )


@tool(mcp, read_only=True, name="ares_subjekt_rzp")
def lookup_rzp(ico: str) -> SubjektRzpResult:
    """Živnosti a provozovny subjektu ze živnostenského rejstříku (podle IČO).

    Vrací aktuální předměty podnikání (živnosti) a aktivní provozovny (název +
    adresa). Neobsahuje osobní údaje.
    """
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    payload, resp = _fetch(
        "GET",
        f"{ARES_RZP_BASE_URL}/{ico}",
        not_found_msg=f"IČO {ico} není v živnostenském rejstříku",
    )
    try:
        zaznamy = _maps(payload.get("zaznamy"))
        if not zaznamy:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT, f"IČO {ico} nemá záznam v živnostenském rejstříku"
            )
        data, warnings = _reduce_rzp(zaznamy[0])
    except ConnectorError:
        raise
    except _SCHEMA_EXC as e:
        raise _schema_error(e) from None

    return SubjektRzpResult(data=data, provenance=_provenance(resp), warnings=warnings)


def _reduce_rzp(zaznam: dict[str, Any]) -> tuple[SubjektRzpData, list[str]]:
    """Zredukuje RŽP záznam — jen **aktuální** (nezaniklé) živnosti a
    provozovny. Provozovny jsou vnořené pod každou živností a napříč nimi
    se opakují → deduplikace podle `icp`. Osoby se záměrně nenesou (PII).

    Vrací i `warnings`: obě pole jdou přes strop (`MAX_ZIVNOSTI`,
    `MAX_PROVOZOVEN`) a upozornění nese skutečný počet před oříznutím —
    volající sám nemá jak zjistit, kolik jich po deduplikaci bylo.
    """
    zivnosti: list[ZivnostItem] = []
    provozovny: dict[Any, ProvozovnaItem] = {}

    def _sber_provozoven(zdroj: object) -> None:
        for p in _maps(zdroj):
            if p.get("platnostDo"):  # zrušená provozovna
                continue
            icp = p.get("icp")
            # `icp` musí být hashovatelné; objekt/pole na jeho místě by
            # shodilo dedup slovník na TypeError.
            key = icp if isinstance(icp, (str, int, float)) else id(p)
            if key in provozovny:
                continue
            provozovny[key] = ProvozovnaItem(
                nazev=_text(p.get("nazev")),
                adresa=_text(_map(p.get("sidloProvozovny")).get("textovaAdresa")),
                typ=_text(p.get("typProvozovny")),
            )

    for zi in _maps(zaznam.get("zivnosti")):
        if zi.get("datumZaniku"):  # zaniklá živnost
            continue
        predmet = _text(zi.get("predmetPodnikani"))
        if predmet:
            zivnosti.append(ZivnostItem(predmet=predmet, druh=_text(zi.get("druhZivnosti"))))
        _sber_provozoven(zi.get("provozovny"))

    _sber_provozoven(zaznam.get("provozovny"))  # niekedy aj top-level

    vsechny_provozovny = list(provozovny.values())
    warnings: list[str] = []
    if len(zivnosti) > MAX_ZIVNOSTI:
        warnings.append(
            f"Subjekt má {len(zivnosti)} aktuálních živností, vráceno prvních {MAX_ZIVNOSTI}."
        )
    if len(vsechny_provozovny) > MAX_PROVOZOVEN:
        warnings.append(
            f"Subjekt má {len(vsechny_provozovny)} aktivních provozoven, "
            f"vráceno prvních {MAX_PROVOZOVEN}."
        )

    return (
        SubjektRzpData(
            ico=_text(zaznam.get("ico")),
            obchodni_jmeno=_text(zaznam.get("obchodniJmeno")),
            pravni_forma=_text(zaznam.get("pravniForma")),
            zivnosti=zivnosti[:MAX_ZIVNOSTI],
            provozovny=vsechny_provozovny[:MAX_PROVOZOVEN],
        ),
        warnings,
    )


@tool(mcp, read_only=True, name="ares_subjekt_res")
def lookup_res(ico: str) -> SubjektResResult:
    """Statistické údaje subjektu z registru ekonomických subjektů (RES) podle IČO.

    Doplňuje k základnímu detailu klasifikaci NACE a kategorii počtu zaměstnanců.
    """
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    payload, resp = _fetch(
        "GET",
        f"{ARES_RES_BASE_URL}/{ico}",
        not_found_msg=f"IČO {ico} není v registru ekonomických subjektů",
    )
    try:
        zaznamy = _maps(payload.get("zaznamy"))
        if not zaznamy:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT, f"IČO {ico} nemá záznam v RES"
            )
        z = zaznamy[0]
        stat = _map(z.get("statistickeUdaje"))
        sidlo_raw = z.get("sidlo")
        nace = _texts(z.get("czNace"))
        data = SubjektResData(
            ico=_text(z.get("ico")),
            obchodni_jmeno=_text(z.get("obchodniJmeno")),
            pravni_forma=_text(z.get("pravniForma")),
            sidlo=Sidlo(**sidlo_raw) if isinstance(sidlo_raw, dict) else None,
            cz_nace=nace[:MAX_VNORENYCH_POLOZEK],
            kategorie_poctu_pracovniku=_text(stat.get("kategoriePoctuPracovniku")),
            institucionalni_sektor=_text(stat.get("institucionalniSektor2010")),
        )
    except ConnectorError:
        raise
    except _SCHEMA_EXC as e:
        raise _schema_error(e) from None

    warnings: list[str] = []
    if len(nace) > MAX_VNORENYCH_POLOZEK:
        warnings.append(
            f"Subjekt má {len(nace)} kódů NACE, vráceno prvních {MAX_VNORENYCH_POLOZEK}."
        )

    return SubjektResResult(data=data, provenance=_provenance(resp), warnings=warnings)


@tool(mcp, read_only=True, name="ares_subjekt_nrpzs")
def lookup_nrpzs(ico: str) -> SubjektNrpzsResult:
    """Zdravotnická zařízení subjektu z Národního registru poskytovatelů
    zdravotních služeb (NRPZS) podle IČO.

    Vrací seznam zařízení/pracovišť: název, druh (kód — přeložit přes
    ares_ciselnik, kod_ciselniku='DruhZarizeni', zdroj='nrpzs'), adresu a
    institucionální kontakty (telefon, e-mail, web). Neobsahuje osobní údaje.
    """
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    payload, resp = _fetch(
        "GET",
        f"{ARES_NRPZS_BASE_URL}/{ico}",
        not_found_msg=(
            f"IČO {ico} není v NRPZS (subjekt není registrovaným "
            "poskytovatelem zdravotních služeb)"
        ),
    )
    try:
        zaznamy = _maps(payload.get("zaznamy"))
        if not zaznamy:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT, f"IČO {ico} nemá záznam v NRPZS"
            )
        data = _reduce_nrpzs(zaznamy)
    except ConnectorError:
        raise
    except _SCHEMA_EXC as e:
        raise _schema_error(e) from None

    warnings: list[str] = []
    if len(zaznamy) > MAX_ZARIZENI:
        warnings.append(
            f"Subjekt má {len(zaznamy)} zařízení/pracovišť, vráceno prvních {MAX_ZARIZENI}."
        )
    return SubjektNrpzsResult(data=data, provenance=_provenance(resp), warnings=warnings)


def _reduce_nrpzs(zaznamy: list[dict[str, Any]]) -> SubjektNrpzsData:
    """Zredukuje NRPZS záznamy (jeden na zařízení/pracoviště) na seznam
    zařízení s institucionálními kontakty. `angazovaneOsoby` se **záměrně
    zahazují** (PII — jména osob podílejících se na řízení)."""
    zarizeni: list[ZarizeniNrpzs] = []
    for z in zaznamy[:MAX_ZARIZENI]:
        kontakty = _map(z.get("kontakty"))
        zarizeni.append(
            ZarizeniNrpzs(
                nazev=_text(z.get("obchodniJmeno")),
                druh_zarizeni=_text(z.get("druhZarizeni")),
                adresa=_text(_map(z.get("sidlo")).get("textovaAdresa")),
                telefon=_text(kontakty.get("telefon")),
                email=_text(kontakty.get("email")),
                www=_text(kontakty.get("www")),
                primarni=_bool(z.get("primarniZaznam")),
            )
        )

    prvni = zaznamy[0]
    return SubjektNrpzsData(
        ico=_text(prvni.get("ico")),
        obchodni_jmeno=_text(prvni.get("obchodniJmeno")),
        pravni_forma=_text(prvni.get("pravniForma")),
        zarizeni=zarizeni,
    )


@tool(mcp, read_only=True, name="ares_ciselnik")
def lookup_ciselnik(
    kod_ciselniku: str,
    zdroj: str | None = None,
    hledat: str | None = None,
    kod: str | None = None,
) -> CiselnikResult:
    """Přeloží číselníkové kódy z ARES odpovědí na názvy (např. PravniForma
    kód 112 → „Společnost s ručením omezeným").

    `kod_ciselniku` je např. PravniForma, DruhZarizeni, TypSubjektu;
    `zdroj` upřesní oblast (res, com, vr, rzp, nrpzs, …), `kod` vrátí jedinou
    položku, `hledat` filtruje názvy podřetězcem. Bez filtru max 50 položek.
    """
    k = (kod_ciselniku or "").strip()
    if not k:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "kod_ciselniku je povinný (např. PravniForma)"
        )
    k = _volny_text(k, "kod_ciselniku", 1, MAX_KOD_ZNAKU)
    z = _volny_text(zdroj, "zdroj", 0, MAX_KOD_ZNAKU)
    kod = _volny_text(kod, "kod", 0, MAX_KOD_ZNAKU)
    hledat = _volny_text(hledat, "hledat", 0, MAX_TEXT_ZNAKU)

    # Stránkování tohoto ARES endpointu je po ČÍSELNÍCÍCH, ne po položkách —
    # `pocet: 10` tedy znamená „až 10 číselníků", ne 10 řádků. Proto se
    # položky filtrují a ořezávají (MAX_CISELNIK_POLOZEK) až tu, po odpovědi.
    filtr: dict[str, Any] = {"kodCiselniku": k, "start": 0, "pocet": 10}
    if z:
        filtr["zdrojCiselniku"] = z

    payload, resp = _fetch(
        "POST", ARES_CISELNIKY_URL, body=filtr, not_found_msg=f"číselník {k} nebyl nalezen"
    )
    try:
        ciselniky = _maps(payload.get("ciselniky"))
        if not ciselniky:
            raise ConnectorError(ErrorCode.INVALID_INPUT, f"číselník {k} nebyl nalezen")

        hl = (hledat or "").strip().lower()
        kd = (kod or "").strip()

        def _filtruj(c: dict[str, Any]) -> list[CiselnikPolozka]:
            out: list[CiselnikPolozka] = []
            for p in _maps(c.get("polozkyCiselniku")):
                pk = _text(p.get("kod"))
                nazev = _nazev_cs(p.get("nazev"))
                if kd and pk != kd:
                    continue
                if hl and hl not in nazev.lower():
                    continue
                out.append(CiselnikPolozka(kod=pk, nazev=nazev))
            return out

        # Stejný kód číselníku může existovat ve více zdrojích (com, res,
        # rzp, …) s různým obsahem — při aktivním filtru vyber první zdroj, kde
        # filtr něco našel (kód 112 je v 'res', ale ne v 'com'); bez filtru
        # zůstává první vrácený.
        c = ciselniky[0]
        polozky = _filtruj(c)
        if (kd or hl) and not polozky:
            for kandidat in ciselniky[1:]:
                najdene = _filtruj(kandidat)
                if najdene:
                    c, polozky = kandidat, najdene
                    break

        warnings: list[str] = []
        if len(ciselniky) > 1:
            zdroje = ", ".join(_text(x.get("zdrojCiselniku")) or "?" for x in ciselniky)
            warnings.append(
                f"Číselník existuje ve více zdrojích ({zdroje}); vrácen "
                f"'{_text(c.get('zdrojCiselniku'))}' — upřesněte parametr 'zdroj'."
            )
    except ConnectorError:
        raise
    except _SCHEMA_EXC as e:
        raise _schema_error(e) from None

    celkem = len(polozky)
    if celkem == 0:
        warnings.append("Žádná položka číselníku neodpovídá zadanému filtru.")
    elif celkem > MAX_CISELNIK_POLOZEK:
        polozky = polozky[:MAX_CISELNIK_POLOZEK]
        warnings.append(
            f"Číselník má {celkem} položek po filtru, vráceno prvních "
            f"{MAX_CISELNIK_POLOZEK} (upřesněte přes 'hledat' nebo 'kod')."
        )

    return CiselnikResult(
        data=CiselnikData(
            kod_ciselniku=k,
            nazev_ciselniku=_text(c.get("nazevCiselniku")),
            zdroj_ciselniku=_text(c.get("zdrojCiselniku")),
            pocet_celkem=celkem,
            polozky=polozky,
        ),
        provenance=_provenance(resp),
        warnings=warnings,
    )


def _nazev_cs(nazvy: object) -> str:
    """Z vícejazyčného pole `nazev` ([{kodJazyka, nazev}]) vybere český název,
    jinak první dostupný. Skalár/prázdno → prázdný string."""
    if not isinstance(nazvy, list):
        return _text(nazvy)
    prvni = ""
    for n in nazvy:
        if not isinstance(n, dict):
            continue
        hodnota = _text(n.get("nazev"))
        if not prvni:
            prvni = hodnota
        if n.get("kodJazyka") == "cs" and hodnota:
            return hodnota
    return prvni


@tool(mcp, read_only=True, name="ares_adresa_standardizovat")
def standardizovat_adresu(text: str, pocet: int = 5) -> AdresaSeznamResult:
    """Standardizuje adresu podle RÚIAN (našeptávač) — z volného textu vrátí
    strukturované adresy.

    `text` min. 3 znaky; `pocet` 1..20. Užitečné pro ověření/normalizaci adresy
    před vyhledáváním subjektu.
    """
    t = _volny_text(text, "adresa", 3, MAX_TEXT_ZNAKU)
    if not 1 <= pocet <= MAX_ADRES:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, f"pocet musí být v rozsahu 1..{MAX_ADRES}"
        )

    filtr = {
        "textovaAdresa": t,
        "typStandardizaceAdresy": ADRESA_TYP_STANDARDIZACE,
        "start": 0,
        "pocet": pocet,
    }
    payload, resp = _fetch(
        "POST", ARES_ADRESY_URL, body=filtr, not_found_msg="ARES standardizace nevrátila výsledek"
    )
    try:
        adresy = [AdresaItem(**it) for it in _maps(payload.get("standardizovaneAdresy"))]
    except _SCHEMA_EXC as e:
        raise _schema_error(e) from None

    warnings: list[str] = []
    # Stejně jako u vyhledávání: strop platí i tehdy, když ARES `pocet`
    # v těle požadavku ignoruje.
    if len(adresy) > pocet:
        warnings.append(
            f"ARES vrátil {len(adresy)} adres místo požadovaných {pocet}; "
            f"odpověď je oříznutá na {pocet}."
        )
        adresy = adresy[:pocet]

    vraceno = len(adresy)
    celkem, dopocteno = _pocet_celkem(payload.get("pocetCelkem"), vraceno)
    if dopocteno:
        warnings.append(CELKEM_NEDUVERYHODNY_WARNING)
    if celkem == 0:
        warnings.append("Žádná adresa neodpovídá zadání.")
    elif celkem > vraceno:
        warnings.append(f"Nalezeno {celkem} adres, vráceno {vraceno} (upřesněte zadání).")

    return AdresaSeznamResult(
        data=AdresaSeznamData(pocet_celkem=celkem, pocet=pocet, adresy=adresy),
        provenance=_provenance(resp),
        warnings=warnings,
    )
