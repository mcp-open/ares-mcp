# Changelog

Všechny podstatné změny konektoru `ares-mcp`. Formát vychází
z [Keep a Changelog](https://keepachangelog.com/cs/1.1.0/), verzování
respektuje [SemVer](https://semver.org/lang/cs/).

## [0.2.2] – 2026-07-31

### Opraveno — bezpečnost chybových hlášek

- **Text výjimky se už nikdy nedostane do chybové zprávy.** Všech osm nástrojů
  lepilo do `internal` hlášky `str(ValidationError)`, který obsahuje
  `input_value=…` — doslovný výřez odpovědi ARES. U `ares_subjekt_vr`
  a `ares_subjekt_nrpzs` to byl obchvat PII minimalizace: datum narození
  a bydliště, která nástroj z výstupu záměrně odstraňuje, se do modelu vrátila
  chybovou cestou. Nově jde ven pevná hláška (`SCHEMA_ERROR_MSG`), do logu jen
  jméno třídy výjimky a `raise … from None` drží původní text i mimo traceback.
  Hlídá to kanárkový test nad každým nástrojem.
- **Nekorektní tvar vnořené struktury je typovaná chyba, ne pád.** Skalár na
  místě objektu (`{"zaznamy": ["…"]}`) shodil pět nástrojů na `AttributeError`,
  kterou `except` větve nechytaly — klient dostal holé `'str' object has no
  attribute 'get'` **bez** `error.code`. Přibyly tvarové kontroly (`_map`,
  `_maps`, `_text`) a `AttributeError`/`IndexError` jsou v odchytávané množině
  jako pojistka.
- **Objekt na místě skaláru se nepřenáší jako Python repr.** `str(hodnota)`
  by z vnořeného objektu udělal jeho repr a propašoval celý podstrom do dat
  pro model; nově se takové pole vrací prázdné.
- **`ares_subjekt_rzp` dostal strop.** Živnosti i provozovny jsou vnořená pole
  bez stránkování — velikost odpovědi tedy neurčuje volající, ale subjekt:
  Česká pošta (IČO 47114983) vracela 1914 provozoven, ~136 kB JSONu v jediné
  odpovědi nástroje. Nově `MAX_ZIVNOSTI`/`MAX_PROVOZOVEN` = 50 a `warnings`
  nese skutečný počet před oříznutím (stejný vzor jako NRPZS a číselníky).
- **`ares_subjekt_vyhledat` a `ares_adresa_standardizovat` vynucují `pocet`
  lokálně**, ne jen prosbou v těle požadavku — kdyby ho ARES ignoroval, byla
  ochrana kontextu jen zdánlivá.
- **`pocet_celkem` říká pravdu.** Chybějící, nečíselný, záporný nebo menší
  `pocetCelkem`, než kolik položek ARES v téže odpovědi poslal, dřív dal
  `pocet_celkem: 0` a warning „Žádný subjekt neodpovídá zadanému filtru" —
  přímý protiklad vrácených dat. Nově se dopočítá z toho, co je vidět, a
  odpověď to řekne. Upozornění na další stránku bere v potaz `start`
  a prázdná stránka za koncem výsledků se rozliší od „nic nenalezeno".
- **IČO je ASCII.** `\d` v Pythonu matchuje i nearabské desítkové číslice
  (`٠١٢`, `०१२`) a `int()` je přečte, takže takové „IČO" prošlo validací
  i kontrolním součtem a odešlo percent-enkódované na ARES — přesně to volání
  navíc, kterému má validace předejít. Vzor je nově `[0-9]{8}`.
- **`ares_subjekt_res` se nerozpadal na znaky.** `czNace` se iterovalo bez
  kontroly typu, takže `"62010"` tiše vrátilo `['6', '2', '0', '1', '0']`
  a objekt své klíče — poškozená data, která model nemá jak rozpoznat. Nově
  je to porušení schématu (`_texts`); kontrolují se i jednotlivé prvky, takže
  vnořený objekt, pole nebo boolean se už tiše nezahodí. Pole s čísly i `null`
  prochází dál.
- **Volný text na vstupu má i horní mez.** `obchodni_jmeno`, `adresa`, `text`,
  `kod_ciselniku`, `zdroj`, `kod` a `hledat` měly jen dolní; 200 kB řetězec
  tak šel beze změny do těla POST požadavku na ARES (400 kB tělo z jediného
  volání nástroje). Nově `MAX_TEXT_ZNAKU` = 255 a `MAX_KOD_ZNAKU` = 64,
  kontrola běží před upstream voláním.
- **`pocet_celkem` netruncuje desetinná čísla.** `int(12.9)` bylo 12; nově
  je neceločíselná hodnota „ARES ho nedodal" se stejným upozorněním jako
  chybějící nebo nečíselný počet.
- **NRPZS `primarni` není naivní `bool()`.** `bool("false")` i `bool("0")`
  jsou v Pythonu `True`, stejně jako `bool({...})` — příznak primárního
  záznamu tak mohl tvrdit pravý opak. Nově se bere jen skutečný JSON boolean
  a jeho textová podoba.

### Změněno — ohraničení výstupu

- `ares_subjekt_vr` (statutární orgán, předmět podnikání) a `ares_subjekt_res`
  (seznam NACE) dostaly strop `MAX_VNORENYCH_POLOZEK` = 200 s pravdivým
  upozorněním, stejným vzorem jako RŽP a NRPZS. **Pozor na motivaci:** na
  rozdíl od RŽP tady velký payload z živých dat prokázaný **není** — vzorek
  313 reálných subjektů dal maximálně 13 statutárů, 26 předmětů a 23 NACE.
  Strop je proto pojistka proti neohraničené upstream odpovědi, je řádově
  vyšší než pozorovaná maxima a na reálných datech se neuplatní.

### Změněno

- `sdk-canary.yml` používá checkout cestu odvozenou od `run_id` a
  `run_attempt` a po doběhnutí post-akce checkoutu uklízí jen přesně dvě
  cesty tohoto běhu. Fixní cesta na self-hosted runneru zůstala jako neplatný
  checkout a `actions/checkout` končil před testy na `git config --local`
  (`not a git repository`, exit 128); unikátní cesta další běhy od tohoto
  stavu izoluje bez širokého mazání workspace. Cleanup běží v připnutém
  kontejneru a při nemožnosti odstranit přesně pojmenovaný cíl selže; hostitelský
  Python totiž po úspěšném canary nedokázal odstranit jeho `*.egg-info`
  (`PermissionError`) a pouze zanechal warning.
- SDK pin bumpnut na `0d36cf1a93c870fe237ecbe3bee7b52b202df18d`
  (openmcp-sdk 0.4.3) ve všech čtyřech místech — `pyproject.toml`, `.sdk-ref`,
  `release/vendor/` a `SDK_REF` v `tests/test_release_contract.py`. Přináší
  odregistrování nástrojů přes FastMCP `local_provider` (bez deprecation
  varování při startu) a zpřísněné same-origin přesměrování (neplatný port
  a userinfo v `Location`). Externí závislosti SDK se nezměnily, takže
  `release/*.lock` zůstávají beze změny.
- `tests/test_tool_surface.py` — nová sada přes in-memory `fastmcp.Client`:
  osm read-only nástrojů, tvar chyby tak, jak ho vidí klient, a PII
  minimalizace na skutečném výstupu nástroje.
- `docs/COMPLIANCE.md` a `tests/test_manifest.py` doplněny o dva závěry
  auditu: chybová cesta spadá pod zákaz logování provider payloadu, a
  **credential owner scope je pro ARES neaplikovatelný** — bez `credentials`
  a `user_config` control plane žádného vlastníka nevydává a SDK owner
  hlavičky bez `credential_version` odmítá.

## [Nevydáno → součást 0.2.2] – migrace na openmcp-sdk 0.4

### Změněno — migrace na openmcp-sdk 0.4

Vnější plocha konektoru se **nemění**: stejných 8 nástrojů, stejná jména,
stejná vstupní schémata i popisy. Změny jsou uvnitř.

- **Balík přejmenován `mcp_ares` → `connector`**, entrypoint je nově
  `python -m connector` (`src/connector/__main__.py`). Ares byl poslední
  konektor s vlastním jménem balíku; sjednocení znamená, že se šablona
  a build kontext chovají všude stejně.
- **Zrušeny wrappery nad business funkcemi.** Každý nástroj existoval dvakrát
  — business funkce a tenký `@mcp.tool` wrapper — s duplikovaným podpisem
  i docstringem, takže **test cvičil jiný objekt, než byl zaregistrovaný**.
  Nově `@tool(mcp, read_only=True, name="ares_…")` přímo na business funkci;
  dekorátor vrací původní funkci, takže negativní schema testy ji volají dál
  přímo. Ubylo ~90 řádků.
- **`logging.setup()` na úrovni importu odstraněn** — od SDK 0.4 ho volá
  `run_connector` sám. Volání při importu je špatná vrstva: přepsalo by
  konfiguraci komukoli, kdo si modul jen naimportuje.
- `sdk_min_version: 0.4.0`, takže startovní kontroly (`display.tools`
  ↔ registrované nástroje, explicitní `readOnlyHint`, `supports_write`) jsou
  nově **tvrdé**, ne jen varování.
- Přidán `tests/test_conformance.py` (sada ze SDK) a `tests/test_packaging.py`.
  Z `test_manifest.py` zmizely tři testy, které tím byly duplicitní; zůstaly
  jen ares-specifické invarianty (no-secret tvar, žádný PII salt, egress).
- `test_runtime.py` používá reálnou `mcp` instanci místo stubu s prázdným
  seznamem nástrojů — ten by nové kontroly shodily právem.
- CI sjednoceno se šablonou: ruff, mypy `--strict`, `openmcp-sdk validate`
  a smoke test image. Kód na to bylo potřeba doladit (chybějící generické
  parametry u `dict`/`list`, `zip(strict=True)`).

- **HTTP vrstva je `openmcp_sdk.http.UpstreamClient.`** Vlastní `_get`/`_post`
  /`_call` zmizely; pět „různých base URL" byla ve skutečnosti jedna
  (`…/rest`) a pět cest pod ní. Doménové zůstalo jen mapování 404 na hlášku
  konkrétního registru a vrácení `httpx.Response`, aby `provenance` nesla
  skutečně volanou URL. Krátký timeout a `max_attempts=1` se předávají
  konstruktorem. *(Dřívější znění této poznámky tvrdilo opak — popisovalo
  stav, který do vydání nedožil.)*

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
