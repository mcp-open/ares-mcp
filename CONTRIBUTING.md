# Jak přispět

## Příprava prostředí

SDK se neinstaluje z PyPI (jméno tam patří nesouvisejícímu projektu), takže
buď z gitu podle pinu v `pyproject.toml`, nebo z vendorovaného snapshotu:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'                      # SDK z GitHubu podle pinu
# nebo offline:
python release/materialize_sdk.py --root . --output /tmp/openmcp-sdk
pip install /tmp/openmcp-sdk -e '.[test]'
```

## Před odesláním změny

```bash
ruff check src tests
mypy src
openmcp-sdk validate connector.yaml
python -m pytest tests -q
```

Všechny čtyři musí projít — přesně totéž běží v CI.

## Konvence

- **Výchozí větev je `main`.** PR se testuje, ale nebuilduje.
- Komentáře, docstringy i dokumentace jsou **česky**.
- Nový nástroj potřebuje: registraci s `ToolAnnotations(readOnlyHint=True)`,
  záznam v `display.tools` včetně obou locales (`cs` i `sk`) a test, který ho
  volá přímo.
- Envelope na každém nástroji — `provenance` říká, odkud data jsou, `warnings`
  o tom, jestli nejsou oříznutá.
- ARES je veřejné API a konektor je **výhradně čtecí**. Zapisující nástroj sem
  nepatří; kdyby vznikl, `capabilities.supports_write` i `egress.methods` by
  se musely měnit vědomě a s vysvětlením.
- Do repozitáře nepatří tajemství, `.env`, produkční logy ani cizí API
  specifikace.

## Bump SDK

SDK je připnuté na jeden commit, který musí souhlasit na **třech místech**:
`pyproject.toml`, `.sdk-ref` a název archivu v `release/vendor/`. Shodu hlídá
`tests/test_sdk_pin.py`, takže bump znamená změnit všechna tři naráz.

Noční `sdk-canary` workflow běží proti SDK `main` a otevře issue, když se pin
rozejde — aby se rozdíl neobjevil až při bumpu, kdy je největší.

## Změny, které potřebují poznámku v CHANGELOG.md

Manifest, tvar odpovědi nástroje, rozsah vracených údajů z veřejného rejstříku
a jakákoli změna, kterou pozná uživatel.

## Bezpečnostní hranice

Tyhle věci nejsou kosmetika a review si na ně dává pozor:

- `readOnlyHint` — SDK fail-closed odregistruje každý nástroj bez ní; chybějící
  anotace znamená, že nástroj v produkci tiše zmizí;
- datum narození a bydliště statutárních osob **nikdy** neopouštějí konektor,
  i když je ARES v odpovědi pošle;
- tělo upstream odpovědi nesmí jít do chybové zprávy pro model;
- do URL patří jen ověřené segmenty, nikdy surový vstup od modelu;
- `egress` v manifestu musí pokrývat vše, na co klient sahá.
