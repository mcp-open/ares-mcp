"""Pydantic modely pro ARES connector — vstup i strukturovaný výstup.

`SubjektData` je záměrně redukovaný výřez skutečné ARES odpovědi (REST
`ekonomicke-subjekty/{ico}`) — nese pole, která US-01 (Petra, účetní) a
podobné user stories z konceptu potřebují k ověření firmy podle IČO, ne
celý ARES payload (desítky polí seznamRegistraci/dalsiUdaje nejsou součástí
kontraktu tohoto WP).
"""

from __future__ import annotations

from openmcp_sdk.envelope import EnvelopeBase
from pydantic import BaseModel, ConfigDict, Field


class Sidlo(BaseModel):
    """Sídlo ekonomického subjektu — redukovaný výřez ARES `sidlo` objektu."""

    model_config = ConfigDict(populate_by_name=True)

    textova_adresa: str = Field(alias="textovaAdresa", default="")
    nazev_obce: str = Field(alias="nazevObce", default="")
    psc: int | None = Field(alias="psc", default=None)


class SubjektData(BaseModel):
    """Ekonomický subjekt podle IČO — redukovaný výřez ARES odpovědi.

    Aliasy odpovídají přesně polím ARES REST API, aby `SubjektData(**json)`
    fungovalo přímo nad upstream odpovědí bez ručního mapování.
    """

    model_config = ConfigDict(populate_by_name=True)

    ico: str
    obchodni_jmeno: str = Field(alias="obchodniJmeno")
    pravni_forma: str = Field(alias="pravniForma", default="")
    datum_vzniku: str | None = Field(alias="datumVzniku", default=None)
    dic: str | None = Field(default=None)
    sidlo: Sidlo
    cz_nace: list[str] = Field(alias="czNace", default_factory=list)
    # Seznam registrů s AKTIVNÍM záznamem (odvozené ze `seznamRegistraci`,
    # plní `server.lookup_subjekt`) — navádí LLM, který follow-up nástroj má
    # smysl volat (vr → ares_subjekt_vr, rzp → ares_subjekt_rzp, …).
    registrace: list[str] = Field(default_factory=list)


class SubjektResult(EnvelopeBase):
    """Výstup `ares_subjekt_lookup` — `data` + zděděné `provenance`/`warnings`."""

    data: SubjektData


class SubjektSummary(BaseModel):
    """Jedna položka výsledku vyhledávání (`/ekonomicke-subjekty/vyhledat`).

    Redukovaný výřez — na rozdíl od single-lookupu položka seznamu nemusí mít
    `ico` (zahraniční subjekty nesou pouze `icoId` jako `ARES_...`), proto je
    `ico` volitelné. Extra pole z ARES (financniUrad, czNace, seznamRegistraci, …)
    Pydantic ignoruje. `SubjektSummary(**item)` funguje přímo nad položkou.
    """

    model_config = ConfigDict(populate_by_name=True)

    ico: str | None = None
    obchodni_jmeno: str = Field(alias="obchodniJmeno")
    pravni_forma: str = Field(alias="pravniForma", default="")
    sidlo: Sidlo | None = None


class SubjektSeznamData(BaseModel):
    """Stránka výsledku vyhledávání — echo stránkování + `pocet_celkem`, aby
    LLM vědělo o oříznutí (ARES vrátí jen `pocet` položek z `pocet_celkem`)."""

    pocet_celkem: int
    start: int
    pocet: int
    subjekty: list[SubjektSummary] = Field(default_factory=list)


class SubjektSeznamResult(EnvelopeBase):
    """Výstup `ares_subjekt_vyhledat`."""

    data: SubjektSeznamData


class StatutarniClen(BaseModel):
    """PII-minimalizovaný člen statutárního orgánu.

    Nese pouze **jméno + funkci + název orgánu** — jméno je veřejný údaj
    obchodního rejstříku a je účelem tohoto nástroje (kdo firmu zastupuje).
    `datumNarozeni`, adresa bydliště a státní občanství, které ARES vrací,
    se do LLM **záměrně nepřenášejí** (viz `connector.server._reduce_vr` a
    trvalé PII varování v `SubjektVrResult.warnings`).
    """

    jmeno: str
    funkce: str = ""
    organ: str = ""


class SubjektVrData(BaseModel):
    """Redukovaná data z Veřejného (obchodního) rejstříku pro dané IČO —
    aktuální (nevymazaní) statutáři a aktuální předmět podnikání."""

    ico: str
    obchodni_jmeno: str = ""
    pravni_forma: str = ""
    spisova_znacka: str = ""
    statutarni_organ: list[StatutarniClen] = Field(default_factory=list)
    predmet_podnikani: list[str] = Field(default_factory=list)


class SubjektVrResult(EnvelopeBase):
    """Výstup `ares_subjekt_vr` — vždy nese PII varování ve `warnings`."""

    data: SubjektVrData


class ZivnostItem(BaseModel):
    """Jedna živnost — předmět + druh (R=řemeslná, V=volná, O=vázaná,
    K/Z=koncesovaná; kód `druhZivnosti` z ARES ponechán surový)."""

    predmet: str
    druh: str = ""


class ProvozovnaItem(BaseModel):
    """Jedna provozovna — název + textová adresa. Bez PII."""

    nazev: str = ""
    adresa: str = ""
    typ: str = ""


class SubjektRzpData(BaseModel):
    """Redukovaná data ze Živnostenského rejstříku — aktuální živnosti a
    provozovny. Osoby (`angazovaneOsoby`/`odpovedniZastupci`) se **nenesou**
    (PII), tento nástroj je o předmětu podnikání a provozovnách, ne o lidech."""

    ico: str
    obchodni_jmeno: str = ""
    pravni_forma: str = ""
    zivnosti: list[ZivnostItem] = Field(default_factory=list)
    provozovny: list[ProvozovnaItem] = Field(default_factory=list)


class SubjektRzpResult(EnvelopeBase):
    """Výstup `ares_subjekt_rzp`."""

    data: SubjektRzpData


class SubjektResData(BaseModel):
    """Redukovaná data z Registru ekonomických subjektů (RES) — přidává NACE
    a kategorii počtu zaměstnanců nad rámec agregovaného lookupu."""

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
    """Jedna standardizovaná (RÚIAN) adresa. `AdresaItem(**item)` funguje přímo
    nad položkou ARES; extra pole (kódy krajů/obcí, …) Pydantic ignoruje."""

    model_config = ConfigDict(populate_by_name=True)

    textova_adresa: str = Field(alias="textovaAdresa", default="")
    nazev_obce: str = Field(alias="nazevObce", default="")
    nazev_ulice: str = Field(alias="nazevUlice", default="")
    cislo_domovni: int | None = Field(alias="cisloDomovni", default=None)
    psc: int | None = Field(alias="psc", default=None)
    kod_adresniho_mista: int | None = Field(alias="kodAdresnihoMista", default=None)


class AdresaSeznamData(BaseModel):
    """Výsledek standardizace adresy — echo `pocet` + `pocet_celkem`."""

    pocet_celkem: int
    pocet: int
    adresy: list[AdresaItem] = Field(default_factory=list)


class AdresaSeznamResult(EnvelopeBase):
    """Výstup `ares_adresa_standardizovat`."""

    data: AdresaSeznamData


class CiselnikPolozka(BaseModel):
    """Jedna položka číselníku — kód + český název. Historické (už neplatné)
    položky se nesou také: starší subjekty jejich kódy stále používají."""

    kod: str
    nazev: str = ""


class CiselnikData(BaseModel):
    """Položky jednoho ARES číselníku (např. `PravniForma`) — překlad kódů
    z odpovědí ostatních nástrojů na lidské názvy. `pocet_celkem` je počet
    položek po aplikování filtru (před oříznutím na strop)."""

    kod_ciselniku: str
    nazev_ciselniku: str = ""
    zdroj_ciselniku: str = ""
    pocet_celkem: int
    polozky: list[CiselnikPolozka] = Field(default_factory=list)


class CiselnikResult(EnvelopeBase):
    """Výstup `ares_ciselnik`."""

    data: CiselnikData


class ZarizeniNrpzs(BaseModel):
    """Jedno zdravotnické zařízení/pracoviště z NRPZS. `druh_zarizeni` je
    kód — přeložitelný přes `ares_ciselnik` (kodCiselniku `DruhZarizeni`,
    zdroj `nrpzs`). Kontakty jsou institucionální (recepce/ředitelství),
    ne osobní."""

    nazev: str = ""
    druh_zarizeni: str = ""
    adresa: str = ""
    telefon: str = ""
    email: str = ""
    www: str = ""
    primarni: bool = False


class SubjektNrpzsData(BaseModel):
    """Redukovaná data z Národního registru poskytovatelů zdravotních
    služeb. Angažované osoby (`angazovaneOsoby`) se **záměrně nenesou**
    (PII) — nástroj je o zařízeních a jejich kontaktech, ne o lidech."""

    ico: str
    obchodni_jmeno: str = ""
    pravni_forma: str = ""
    zarizeni: list[ZarizeniNrpzs] = Field(default_factory=list)


class SubjektNrpzsResult(EnvelopeBase):
    """Výstup `ares_subjekt_nrpzs`."""

    data: SubjektNrpzsData
