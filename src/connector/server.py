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
ICO_RE = re.compile(r"^\d{8}$")

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
    except (ValueError, ValidationError, TypeError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

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

    filtr: dict[str, Any] = {"obchodniJmeno": jmeno, "start": start, "pocet": pocet}
    adr = (adresa or "").strip()
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
        return str(x.get("hodnota") or "")
    return str(x) if x is not None else ""


def _reduce_vr(zaznam: dict[str, Any]) -> SubjektVrData:
    """Zredukuje jeden VR záznam na PII-minimalizovaný tvar.

    Filtruje jen **aktuální** (nevymazané, bez `datumVymazu`) statutární orgány,
    jejich členy a předměty podnikání. Z členů nese jen jméno + funkci — datum
    narození ani adresu bydliště (`fyzickaOsoba.datumNarozeni`/`.adresa`) NE.
    Právnická osoba jako člen se nese svým obchodním jménem. Identitní pole
    (`ico`/`obchodniJmeno`/…) nese VR jako historii — bereme aktuální hodnotu.
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
        zaznamy = payload.get("zaznamy") or []
        if not zaznamy:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT, f"IČO {ico} nemá záznam v živnostenském rejstříku"
            )
        data = _reduce_rzp(zaznamy[0])
    except ConnectorError:
        raise
    except (ValueError, ValidationError, TypeError, KeyError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    return SubjektRzpResult(data=data, provenance=_provenance(resp), warnings=[])


def _reduce_rzp(zaznam: dict[str, Any]) -> SubjektRzpData:
    """Zredukuje RŽP záznam — jen **aktuální** (nezaniklé) živnosti a
    provozovny. Provozovny jsou vnořené pod každou živností a napříč nimi
    se opakují → deduplikace podle `icp`. Osoby se záměrně nenesou (PII)."""
    zivnosti: list[ZivnostItem] = []
    provozovny: dict[Any, ProvozovnaItem] = {}

    def _sber_provozoven(zdroj: list[Any]) -> None:
        for p in zdroj or []:
            if p.get("platnostDo"):  # zrušená provozovna
                continue
            icp = p.get("icp")
            key = icp if icp is not None else id(p)
            if key in provozovny:
                continue
            provozovny[key] = ProvozovnaItem(
                nazev=p.get("nazev") or "",
                adresa=(p.get("sidloProvozovny") or {}).get("textovaAdresa") or "",
                typ=str(p.get("typProvozovny") or ""),
            )

    for zi in zaznam.get("zivnosti") or []:
        if zi.get("datumZaniku"):  # zaniklá živnost
            continue
        predmet = zi.get("predmetPodnikani") or ""
        if predmet:
            zivnosti.append(ZivnostItem(predmet=predmet, druh=zi.get("druhZivnosti") or ""))
        _sber_provozoven(zi.get("provozovny") or [])

    _sber_provozoven(zaznam.get("provozovny") or [])  # niekedy aj top-level

    return SubjektRzpData(
        ico=zaznam.get("ico") or "",
        obchodni_jmeno=zaznam.get("obchodniJmeno") or "",
        pravni_forma=zaznam.get("pravniForma") or "",
        zivnosti=zivnosti,
        provozovny=list(provozovny.values()),
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
        zaznamy = payload.get("zaznamy") or []
        if not zaznamy:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT, f"IČO {ico} nemá záznam v RES"
            )
        z = zaznamy[0]
        stat = z.get("statistickeUdaje") or {}
        sidlo_raw = z.get("sidlo")
        data = SubjektResData(
            ico=z.get("ico") or "",
            obchodni_jmeno=z.get("obchodniJmeno") or "",
            pravni_forma=z.get("pravniForma") or "",
            sidlo=Sidlo(**sidlo_raw) if isinstance(sidlo_raw, dict) else None,
            cz_nace=[str(x) for x in (z.get("czNace") or [])],
            kategorie_poctu_pracovniku=str(stat.get("kategoriePoctuPracovniku") or ""),
            institucionalni_sektor=str(stat.get("institucionalniSektor2010") or ""),
        )
    except ConnectorError:
        raise
    except (ValueError, ValidationError, TypeError, KeyError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    return SubjektResResult(data=data, provenance=_provenance(resp), warnings=[])


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
        zaznamy = payload.get("zaznamy") or []
        if not zaznamy:
            raise ConnectorError(
                ErrorCode.INVALID_INPUT, f"IČO {ico} nemá záznam v NRPZS"
            )
        data = _reduce_nrpzs(zaznamy)
    except ConnectorError:
        raise
    except (ValueError, ValidationError, TypeError, KeyError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    warnings: list[str] = []
    if len(zaznamy) > MAX_ZARIZENI:
        warnings.append(
            f"Subjekt má {len(zaznamy)} zařízení/pracovišť, vráceno prvních {MAX_ZARIZENI}."
        )
    return SubjektNrpzsResult(data=data, provenance=_provenance(resp), warnings=warnings)


def _reduce_nrpzs(zaznamy: list[Any]) -> SubjektNrpzsData:
    """Zredukuje NRPZS záznamy (jeden na zařízení/pracoviště) na seznam
    zařízení s institucionálními kontakty. `angazovaneOsoby` se **záměrně
    zahazují** (PII — jména osob podílejících se na řízení)."""
    zarizeni: list[ZarizeniNrpzs] = []
    for z in zaznamy[:MAX_ZARIZENI]:
        kontakty = z.get("kontakty") or {}
        zarizeni.append(
            ZarizeniNrpzs(
                nazev=z.get("obchodniJmeno") or "",
                druh_zarizeni=str(z.get("druhZarizeni") or ""),
                adresa=(z.get("sidlo") or {}).get("textovaAdresa") or "",
                telefon=kontakty.get("telefon") or "",
                email=kontakty.get("email") or "",
                www=kontakty.get("www") or "",
                primarni=bool(z.get("primarniZaznam")),
            )
        )

    prvni = zaznamy[0]
    return SubjektNrpzsData(
        ico=prvni.get("ico") or "",
        obchodni_jmeno=prvni.get("obchodniJmeno") or "",
        pravni_forma=str(prvni.get("pravniForma") or ""),
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

    # Stránkování tohoto ARES endpointu je po ČÍSELNÍCÍCH, ne po položkách —
    # `pocet: 10` tedy znamená „až 10 číselníků", ne 10 řádků. Proto se
    # položky filtrují a ořezávají (MAX_CISELNIK_POLOZEK) až tu, po odpovědi.
    filtr: dict[str, Any] = {"kodCiselniku": k, "start": 0, "pocet": 10}
    z = (zdroj or "").strip()
    if z:
        filtr["zdrojCiselniku"] = z

    payload, resp = _fetch(
        "POST", ARES_CISELNIKY_URL, body=filtr, not_found_msg=f"číselník {k} nebyl nalezen"
    )
    try:
        ciselniky = payload.get("ciselniky") or []
        if not ciselniky:
            raise ConnectorError(ErrorCode.INVALID_INPUT, f"číselník {k} nebyl nalezen")

        hl = (hledat or "").strip().lower()
        kd = (kod or "").strip()

        def _filtruj(c: dict[str, Any]) -> list[CiselnikPolozka]:
            out: list[CiselnikPolozka] = []
            for p in c.get("polozkyCiselniku") or []:
                pk = str(p.get("kod") or "")
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
            zdroje = ", ".join(str(x.get("zdrojCiselniku") or "?") for x in ciselniky)
            warnings.append(
                f"Číselník existuje ve více zdrojích ({zdroje}); vrácen "
                f"'{c.get('zdrojCiselniku')}' — upřesněte parametr 'zdroj'."
            )
    except ConnectorError:
        raise
    except (ValueError, ValidationError, TypeError, KeyError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

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
            nazev_ciselniku=c.get("nazevCiselniku") or "",
            zdroj_ciselniku=str(c.get("zdrojCiselniku") or ""),
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
        return str(nazvy) if nazvy else ""
    prvni = ""
    for n in nazvy:
        if not isinstance(n, dict):
            continue
        hodnota = n.get("nazev") or ""
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
    t = (text or "").strip()
    if len(t) < 3:
        raise ConnectorError(ErrorCode.INVALID_INPUT, "adresa musí mít alespoň 3 znaky")
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
        celkem = int(payload.get("pocetCelkem", 0))
        items = payload.get("standardizovaneAdresy") or []
        adresy = [AdresaItem(**it) for it in items]
    except (ValueError, ValidationError, TypeError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    warnings: list[str] = []
    if celkem == 0:
        warnings.append("Žádná adresa neodpovídá zadání.")
    elif celkem > len(adresy):
        warnings.append(f"Nalezeno {celkem} adres, vráceno {len(adresy)} (upřesněte zadání).")

    return AdresaSeznamResult(
        data=AdresaSeznamData(pocet_celkem=celkem, pocet=pocet, adresy=adresy),
        provenance=_provenance(resp),
        warnings=warnings,
    )
