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
ARES_BASE_URL = f"{_ARES_REST}/ekonomicke-subjekty"
# Samostatné registre — nesú dáta, ktoré agregovaný `ekonomicke-subjekty/{ico}`
# neobsahuje: VR (štatutárny orgán, predmet podnikania), RŽP (živnosti,
# prevádzkarne), RES (NACE, kategória počtu zamestnancov).
ARES_VR_BASE_URL = f"{_ARES_REST}/ekonomicke-subjekty-vr"
ARES_RZP_BASE_URL = f"{_ARES_REST}/ekonomicke-subjekty-rzp"
ARES_RES_BASE_URL = f"{_ARES_REST}/ekonomicke-subjekty-res"
ARES_NRPZS_BASE_URL = f"{_ARES_REST}/ekonomicke-subjekty-nrpzs"
ARES_ADRESY_URL = f"{_ARES_REST}/standardizovane-adresy/vyhledat"
ARES_CISELNIKY_URL = f"{_ARES_REST}/ciselniky-nazevniky/vyhledat"

# `typStandardizaceAdresy` je povinný atribut filtra štandardizácie; ARES dovolí
# len UPLNA_STANDARDIZACE | VYHOVUJICI_ADRESY — berieme úplnú štandardizáciu.
ADRESA_TYP_STANDARDIZACE = "UPLNA_STANDARDIZACE"
MAX_ADRES = 20

# Stropy pre číselníky a NRPZS zariadenia — rovnaká motivácia ako MAX_POCET:
# ochrana LLM kontextu (PravniForma má ~300 položiek, nemocničná sieť môže mať
# desiatky pracovísk; warning v odpovedi povie o orezaní).
MAX_CISELNIK_POLOZEK = 50
MAX_ZARIZENI = 50

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
        data.registrace = _aktivni_registrace(payload.get("seznamRegistraci"))
    except (ValueError, ValidationError, TypeError) as e:
        raise ConnectorError(
            ErrorCode.INTERNAL, f"ARES odpověď neodpovídá očekávanému schématu: {e}"
        ) from e

    return SubjektResult(data=data, provenance=_provenance(resp), warnings=[])


def _aktivni_registrace(seznam: object) -> list[str]:
    """Zo `seznamRegistraci` (kľúče `stavZdrojeXxx`) vráti zoznam registrov
    s hodnotou AKTIVNI ako lowercase skratky (`vr`, `res`, `rzp`, `dph`, …) —
    LLM z nich vidí, ktorý follow-up nástroj má zmysel volať."""
    if not isinstance(seznam, dict):
        return []
    prefix = "stavZdroje"
    return sorted(
        k[len(prefix):].lower()
        for k, v in seznam.items()
        if k.startswith(prefix) and v == "AKTIVNI"
    )


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


def lookup_rzp(ico: str) -> SubjektRzpResult:
    """Business logika `ares_subjekt_rzp` — živnosti a provozovny ze
    Živnostenského rejstříku. Bez PII (osoby se nepřenášejí)."""
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    resp = _call(lambda: _get(f"{ARES_RZP_BASE_URL}/{ico}"))
    _raise_for_status(resp, not_found_msg=f"IČO {ico} není v živnostenském rejstříku")

    payload = _json_dict(resp)
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


def _reduce_rzp(zaznam: dict) -> SubjektRzpData:
    """Zredukuje RŽP záznam — iba **aktuálne** (nezaniknuté) živnosti a
    prevádzkarne. Prevádzkarne sú vnorené pod každou živnosťou a naprieč nimi
    sa opakujú → deduplikácia podľa `icp`. Osoby sa zámerne nenesú (PII)."""
    zivnosti: list[ZivnostItem] = []
    provozovny: dict = {}

    def _sber_provozoven(zdroj: list) -> None:
        for p in zdroj or []:
            if p.get("platnostDo"):  # zrušená prevádzkareň
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
        if zi.get("datumZaniku"):  # zaniknutá živnosť
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


def lookup_res(ico: str) -> SubjektResResult:
    """Business logika `ares_subjekt_res` — NACE a kategória počtu zamestnancov
    z Registra ekonomických subjektov (nad rámec agregovaného lookupu)."""
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    resp = _call(lambda: _get(f"{ARES_RES_BASE_URL}/{ico}"))
    _raise_for_status(resp, not_found_msg=f"IČO {ico} není v registru ekonomických subjektů")

    payload = _json_dict(resp)
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


def lookup_nrpzs(ico: str) -> SubjektNrpzsResult:
    """Business logika `ares_subjekt_nrpzs` — zdravotnícke zariadenia subjektu
    z Národného registra poskytovateľov zdravotných služieb. Angažované osoby
    sa nenesú (PII), kontakty sú inštitucionálne."""
    if not ICO_RE.fullmatch(ico) or not ico_checksum(ico):
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "IČO musí mít 8 číslic a platný kontrolní součet"
        )

    resp = _call(lambda: _get(f"{ARES_NRPZS_BASE_URL}/{ico}"))
    _raise_for_status(
        resp,
        not_found_msg=(
            f"IČO {ico} není v NRPZS (subjekt není registrovaným "
            "poskytovatelem zdravotních služeb)"
        ),
    )

    payload = _json_dict(resp)
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


def _reduce_nrpzs(zaznamy: list) -> SubjektNrpzsData:
    """Zredukuje NRPZS záznamy (jeden na zariadenie/pracovisko) na zoznam
    zariadení s inštitucionálnymi kontaktmi. `angazovaneOsoby` sa **zámerne
    zahadzujú** (PII — mená osôb podieľajúcich sa na riadení)."""
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


def lookup_ciselnik(
    kod_ciselniku: str,
    zdroj: str | None = None,
    hledat: str | None = None,
    kod: str | None = None,
) -> CiselnikResult:
    """Business logika `ares_ciselnik` — preklad kódov na názvy. Stránkovanie
    ARES endpointu je po číselníkoch (nie položkách), preto sa položky filtrujú
    a orezávajú až tu (`kod` presná zhoda, `hledat` substring bez diakritiky
    nerozlišuje veľkosť, strop MAX_CISELNIK_POLOZEK)."""
    k = (kod_ciselniku or "").strip()
    if not k:
        raise ConnectorError(
            ErrorCode.INVALID_INPUT, "kod_ciselniku je povinný (např. PravniForma)"
        )

    filtr: dict = {"kodCiselniku": k, "start": 0, "pocet": 10}
    z = (zdroj or "").strip()
    if z:
        filtr["zdrojCiselniku"] = z

    resp = _call(lambda: _post(ARES_CISELNIKY_URL, filtr))
    _raise_for_status(resp, not_found_msg=f"číselník {k} nebyl nalezen")

    payload = _json_dict(resp)
    try:
        ciselniky = payload.get("ciselniky") or []
        if not ciselniky:
            raise ConnectorError(ErrorCode.INVALID_INPUT, f"číselník {k} nebyl nalezen")

        hl = (hledat or "").strip().lower()
        kd = (kod or "").strip()

        def _filtruj(c: dict) -> list[CiselnikPolozka]:
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

        # Rovnaký kod číselníka môže existovať vo viacerých zdrojoch (com, res,
        # rzp, …) s rôznym obsahom — pri aktívnom filtri vyber prvý zdroj, kde
        # filter niečo našiel (kod 112 je v 'res', ale nie v 'com'); bez filtra
        # ostáva prvý vrátený.
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
    """Z viacjazyčného poľa `nazev` ([{kodJazyka, nazev}]) vyberie český názov,
    inak prvý dostupný. Skalár/prázdno → prázdny string."""
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


def standardizovat_adresu(text: str, pocet: int = 5) -> AdresaSeznamResult:
    """Business logika `ares_adresa_standardizovat` — RÚIAN standardizace/
    našeptávač adresy podle volného textu."""
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
    resp = _call(lambda: _post(ARES_ADRESY_URL, filtr))
    _raise_for_status(resp, not_found_msg="ARES standardizace nevrátila výsledek")

    payload = _json_dict(resp)
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_subjekt_lookup(ico: str) -> SubjektResult:
    """Vyhledá ekonomický subjekt v ARES podle IČO (8 číslic).

    Vrací základní detail (jméno, sídlo, právní forma, DIČ, NACE) a pole
    `registrace` — seznam registrů s aktivním záznamem (vr → ares_subjekt_vr,
    rzp → ares_subjekt_rzp, res → ares_subjekt_res, nrpzs → ares_subjekt_nrpzs).
    """
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_subjekt_rzp(ico: str) -> SubjektRzpResult:
    """Živnosti a provozovny subjektu ze živnostenského rejstříku (podle IČO).

    Vrací aktuální předměty podnikání (živnosti) a aktivní provozovny (název +
    adresa). Neobsahuje osobní údaje.
    """
    return lookup_rzp(ico)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_subjekt_res(ico: str) -> SubjektResResult:
    """Statistické údaje subjektu z registru ekonomických subjektů (RES) podle IČO.

    Doplňuje k základnímu detailu klasifikaci NACE a kategorii počtu zaměstnanců.
    """
    return lookup_res(ico)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_subjekt_nrpzs(ico: str) -> SubjektNrpzsResult:
    """Zdravotnická zařízení subjektu z Národního registru poskytovatelů
    zdravotních služeb (NRPZS) podle IČO.

    Vrací seznam zařízení/pracovišť: název, druh (kód — přeložit přes
    ares_ciselnik, kod_ciselniku='DruhZarizeni', zdroj='nrpzs'), adresu a
    institucionální kontakty (telefon, e-mail, web). Neobsahuje osobní údaje.
    """
    return lookup_nrpzs(ico)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_ciselnik(
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
    return lookup_ciselnik(kod_ciselniku, zdroj=zdroj, hledat=hledat, kod=kod)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def ares_adresa_standardizovat(text: str, pocet: int = 5) -> AdresaSeznamResult:
    """Standardizuje adresu podle RÚIAN (našeptávač) — z volného textu vrátí
    strukturované adresy.

    `text` min. 3 znaky; `pocet` 1..20. Užitečné pro ověření/normalizaci adresy
    před vyhledáváním subjektu.
    """
    return standardizovat_adresu(text, pocet=pocet)


def main() -> None:
    # fastmcp 2.x: transport "http" == Streamable HTTP. host/port/path/
    # stateless_http sa v 2.x odovzdávajú do run() (nie do konštruktora) —
    # explicitne uvedené kvôli auditovateľnosti (gateway proxuje na :8000/mcp).
    mcp.run(transport="http", host="0.0.0.0", port=8000, path="/mcp", stateless_http=True)


if __name__ == "__main__":
    main()
