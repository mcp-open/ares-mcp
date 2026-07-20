# Changelog

Všechny podstatné změny konektoru `mcp-ares`. Formát vychází
z [Keep a Changelog](https://keepachangelog.com/cs/1.1.0/), verzování
respektuje [SemVer](https://semver.org/lang/cs/).

## [Nevydáno]

### Opraveno

- `connector.yaml` deklaroval verzi `0.2.0`, zatímco balíček byl na `0.2.1` —
  katalog hlásil starou verzi. Nový test `test_manifest_version_matches_package`
  hlídá, aby se to znovu nerozešlo.
- `_json_dict` neošetřoval `JSONDecodeError` z `resp.json()`. Při HTTP 200
  s ne-JSON tělem (chybová HTML stránka z proxy) chybu zachytil až volající
  přes `except ValueError` — tedy jen náhodou skrz dědičnost. Funkce teď plní
  svůj kontrakt sama a vrací `internal` s jasnou hláškou.

### Přidáno

- `tests/test_manifest.py` — validace manifestu, soulad `display.tools`
  s registrovanými nástroji a kontrola invariantu `supports_test` vs. předání
  `test_connection` do `run_connector`.
- `[project.optional-dependencies] test` — dřív CI instalovalo `pytest`
  ad hoc v run kroku.

### Poznámky

- `supports_test` zůstává vypnuté a je to teď zdokumentované v manifestu:
  ARES nemá credentials, takže `/test` by ověřoval jen dostupnost upstreamu,
  což patří do monitoringu platformy (SPEC-003 `ops.synthetic`).

## [0.2.1] – 2026-07-20

### Opraveno

- `ares_ciselnik`: filtr `kod`/`hledat` prohledá všechny vrácené zdroje
  číselníku, ne jen první — kód 112 (s.r.o.) je ve zdroji `res`, ale ne
  v `com`, takže volání bez `zdroj` dřív vracelo prázdný výsledek.

## [0.2.0] – 2026-07-20

### Přidáno

- Nástroj **`ares_subjekt_nrpzs`** — zdravotnická zařízení subjektu
  z Národního registru poskytovatelů zdravotních služeb: název, druh
  (číselníkový kód), adresa a institucionální kontakty (telefon, e-mail,
  web). Angažované osoby se nepřenášejí (PII minimalizace).
- Nástroj **`ares_ciselnik`** — překlad číselníkových kódů z ARES odpovědí
  na názvy (např. `PravniForma` 112 → „Společnost s ručením omezeným").
  Filtry `zdroj`, `kod` (přesná shoda) a `hledat` (podřetězec), strop
  50 položek s upozorněním na oříznutí.
- `ares_subjekt_lookup` nově vrací **`cz_nace`** (klasifikace činností)
  a **`registrace`** — seznam registrů, kde má subjekt aktivní záznam
  (navádí na follow-up nástroje `vr`/`rzp`/`res`/`nrpzs`).
- **`connector.yaml`** — manifest pro katalog platformy (no-secret,
  read-only, display sekce pro web detail, egress ares.gov.cz), včetně
  `display.tools` — seznam všech 8 MCP nástrojů s popisy pro web detail.
- Tento changelog.

## [0.1.0] – 2026-07-19

### Přidáno

- Iniciální verze no-secret konektoru nad veřejným ARES REST API
  (FastMCP Streamable HTTP, port 8000, cesta `/mcp`).
- Nástroje: `ares_subjekt_lookup` (detail podle IČO s validací kontrolního
  součtu), `ares_subjekt_vyhledat` (fulltext podle obchodního jména se
  stránkováním), `ares_subjekt_vr` (statutární orgán a předmět podnikání
  z veřejného rejstříku, PII minimalizace — jen jméno a funkce),
  `ares_subjekt_rzp` (živnosti a provozovny), `ares_subjekt_res` (NACE
  a kategorie počtu zaměstnanců), `ares_adresa_standardizovat` (RÚIAN
  našeptávač).
- Typované chyby přes `openmcp_sdk` envelope (invalid_input, rate_limited,
  upstream_error, upstream_unavailable, internal), bounded timeout 5 s,
  žádný retry, provenance u každé odpovědi.
