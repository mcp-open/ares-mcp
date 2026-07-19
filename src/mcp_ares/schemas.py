"""Pydantic modely pre ARES connector — vstup aj štruktúrovaný výstup.

`SubjektData` je zámerne redukovaný výrez skutočnej ARES odpovede (REST
`ekonomicke-subjekty/{ico}`) — nesie polia, ktoré US-01 (Petra, účetní) a
podobné user stories z konceptu potrebujú na overenie firmy podľa IČO, nie
celý ARES payload (desiatky polí seznamRegistraci/dalsiUdaje nie sú súčasťou
kontraktu tohto WP).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from openmcp_sdk.envelope import EnvelopeBase


class Sidlo(BaseModel):
    """Sídlo ekonomického subjektu — redukovaný výrez ARES `sidlo` objektu."""

    model_config = ConfigDict(populate_by_name=True)

    textova_adresa: str = Field(alias="textovaAdresa", default="")
    nazev_obce: str = Field(alias="nazevObce", default="")
    psc: int | None = Field(alias="psc", default=None)


class SubjektData(BaseModel):
    """Ekonomický subjekt podľa IČO — redukovaný výrez ARES odpovede.

    Aliasy zodpovedajú presne poľom ARES REST API, aby `SubjektData(**json)`
    fungovalo priamo nad upstream odpoveďou bez ručného mapovania.
    """

    model_config = ConfigDict(populate_by_name=True)

    ico: str
    obchodni_jmeno: str = Field(alias="obchodniJmeno")
    pravni_forma: str = Field(alias="pravniForma", default="")
    datum_vzniku: str | None = Field(alias="datumVzniku", default=None)
    dic: str | None = Field(default=None)
    sidlo: Sidlo


class SubjektResult(EnvelopeBase):
    """Výstup `ares_subjekt_lookup` — `data` + zdedené `provenance`/`warnings`."""

    data: SubjektData


class SubjektSummary(BaseModel):
    """Jedna položka výsledku vyhledávania (`/ekonomicke-subjekty/vyhledat`).

    Redukovaný výrez — na rozdiel od single-lookupu položka zoznamu nemusí mať
    `ico` (zahraničné subjekty nesú iba `icoId` ako `ARES_...`), preto je `ico`
    voliteľné. Extra polia z ARES (financniUrad, czNace, seznamRegistraci, …)
    Pydantic ignoruje. `SubjektSummary(**item)` funguje priamo nad položkou.
    """

    model_config = ConfigDict(populate_by_name=True)

    ico: str | None = None
    obchodni_jmeno: str = Field(alias="obchodniJmeno")
    pravni_forma: str = Field(alias="pravniForma", default="")
    sidlo: Sidlo | None = None


class SubjektSeznamData(BaseModel):
    """Stránka výsledku vyhledávania — echo stránkovania + `pocet_celkem`, aby
    LLM vedelo o orezaní (ARES vráti len `pocet` položiek z `pocet_celkem`)."""

    pocet_celkem: int
    start: int
    pocet: int
    subjekty: list[SubjektSummary] = Field(default_factory=list)


class SubjektSeznamResult(EnvelopeBase):
    """Výstup `ares_subjekt_vyhledat`."""

    data: SubjektSeznamData


class StatutarniClen(BaseModel):
    """PII-minimalizovaný člen štatutárneho orgánu.

    Nesie iba **meno + funkciu + názov orgánu** — meno je verejný údaj
    obchodného registra a je účelom tohto nástroja (kto firmu zastupuje).
    `datumNarozeni`, adresa bydliska a štátne občianstvo, ktoré ARES vracia,
    sa do LLM **zámerne neprenášajú** (viď `mcp_ares.server._reduce_vr` a
    trvalé PII varovanie v `SubjektVrResult.warnings`).
    """

    jmeno: str
    funkce: str = ""
    organ: str = ""


class SubjektVrData(BaseModel):
    """Redukované dáta z Veřejného (obchodného) registra pre dané IČO —
    aktuálni (nevymazaní) štatutári a aktuálny predmet podnikania."""

    ico: str
    obchodni_jmeno: str = ""
    pravni_forma: str = ""
    spisova_znacka: str = ""
    statutarni_organ: list[StatutarniClen] = Field(default_factory=list)
    predmet_podnikani: list[str] = Field(default_factory=list)


class SubjektVrResult(EnvelopeBase):
    """Výstup `ares_subjekt_vr` — vždy nesie PII varovanie vo `warnings`."""

    data: SubjektVrData


class ZivnostItem(BaseModel):
    """Jedna živnosť — predmet + druh (R=řemeslná, V=volná, O=vázaná,
    K/Z=koncesovaná; kód `druhZivnosti` z ARES ponechaný surový)."""

    predmet: str
    druh: str = ""


class ProvozovnaItem(BaseModel):
    """Jedna prevádzkareň — názov + textová adresa. Bez PII."""

    nazev: str = ""
    adresa: str = ""
    typ: str = ""


class SubjektRzpData(BaseModel):
    """Redukované dáta zo Živnostenského registra — aktuálne živnosti a
    prevádzkarne. Osoby (`angazovaneOsoby`/`odpovedniZastupci`) sa **nenesú**
    (PII), tento nástroj je o predmete podnikania a prevádzkach, nie o ľuďoch."""

    ico: str
    obchodni_jmeno: str = ""
    pravni_forma: str = ""
    zivnosti: list[ZivnostItem] = Field(default_factory=list)
    provozovny: list[ProvozovnaItem] = Field(default_factory=list)


class SubjektRzpResult(EnvelopeBase):
    """Výstup `ares_subjekt_rzp`."""

    data: SubjektRzpData


class SubjektResData(BaseModel):
    """Redukované dáta z Registra ekonomických subjektov (RES) — pridáva NACE
    a kategóriu počtu zamestnancov nad rámec agregovaného lookupu."""

    ico: str
    obchodni_jmeno: str = ""
    pravni_forma: str = ""
    sidlo: Sidlo | None = None
    cz_nace: list[str] = Field(default_factory=list)
    kategorie_poctu_pracovniku: str = ""
    institucionalni_sektor: str = ""


class SubjektResResult(EnvelopeBase):
    """Výstup `ares_subjekt_res`."""

    data: SubjektResData


class AdresaItem(BaseModel):
    """Jedna štandardizovaná (RÚIAN) adresa. `AdresaItem(**item)` funguje priamo
    nad položkou ARES; extra polia (kódy krajov/obcí, …) Pydantic ignoruje."""

    model_config = ConfigDict(populate_by_name=True)

    textova_adresa: str = Field(alias="textovaAdresa", default="")
    nazev_obce: str = Field(alias="nazevObce", default="")
    nazev_ulice: str = Field(alias="nazevUlice", default="")
    cislo_domovni: int | None = Field(alias="cisloDomovni", default=None)
    psc: int | None = Field(alias="psc", default=None)
    kod_adresniho_mista: int | None = Field(alias="kodAdresnihoMista", default=None)


class AdresaSeznamData(BaseModel):
    """Výsledok štandardizácie adresy — echo `pocet` + `pocet_celkem`."""

    pocet_celkem: int
    pocet: int
    adresy: list[AdresaItem] = Field(default_factory=list)


class AdresaSeznamResult(EnvelopeBase):
    """Výstup `ares_adresa_standardizovat`."""

    data: AdresaSeznamData
