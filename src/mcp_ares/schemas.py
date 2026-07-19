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
