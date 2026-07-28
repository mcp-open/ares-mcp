# ares-mcp

MCP konektor pro **ARES** — český registr ekonomických subjektů. Ověří firmu
podle IČO, najde ji podle jména, přečte veřejný i živnostenský rejstřík,
standardizuje adresu podle RÚIAN a přeloží číselníkové kódy.

ARES je veřejné API bez přihlášení, takže konektor **nepotřebuje žádné
přihlašovací údaje** a nic nikam nezapisuje. Je zároveň referenční ukázkou,
jak vypadá konektor postavený nad [openmcp-sdk](https://github.com/mcp-open/openmcp-sdk).

Součást platformy [OpenMCP.cz](https://openmcp.cz).

## Rychlý start

Konektor závisí na `openmcp-sdk`, které se neinstaluje z PyPI (jméno tam patří
nesouvisejícímu projektu). Instaluje se z gitu:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
python -m connector
```

`pip` si SDK stáhne z GitHubu podle commitu připnutého v `pyproject.toml`.

Bez sítě k GitHubu — nebo když chcete přesně ten snapshot SDK, se kterým se
staví produkční image — použijte vendorovaný archiv v repozitáři:

```bash
python release/materialize_sdk.py --root . --output /tmp/openmcp-sdk
pip install /tmp/openmcp-sdk -e .
```

Skript ověří SHA-256 archivu i shodu commitu s `.sdk-ref`.

### Připojení do MCP klienta

```json
{
  "mcpServers": {
    "ares": {
      "command": "/cesta/k/.venv/bin/python",
      "args": ["-m", "connector"],
      "cwd": "/cesta/k/ares-mcp"
    }
  }
}
```

Žádné proměnné prostředí nejsou potřeba. `OPENMCP_MODE` má výchozí hodnotu
`local-stdio`, ostatní režimy (`hosted`, `self-hosted`) používá platforma.

Vyzkoušení bez klienta:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python -m connector
```

## Nástroje

Všech osm je **pouze ke čtení**.

| Nástroj | Co dělá |
|---|---|
| `ares_subjekt_lookup` | Detail podle IČO — jméno, sídlo, právní forma, DIČ, NACE a přehled aktivních registrů |
| `ares_subjekt_vyhledat` | Hledání podle obchodního jména, volitelně upřesněné adresou, se stránkováním |
| `ares_subjekt_vr` | Statutární orgán a předmět podnikání z veřejného rejstříku |
| `ares_subjekt_rzp` | Živnosti a provozovny ze živnostenského rejstříku |
| `ares_subjekt_res` | Statistické údaje z RES — klasifikace NACE, kategorie počtu zaměstnanců |
| `ares_subjekt_nrpzs` | Zdravotnická zařízení subjektu z NRPZS včetně institucionálních kontaktů |
| `ares_adresa_standardizovat` | Standardizace adresy z volného textu podle RÚIAN |
| `ares_ciselnik` | Překlad číselníkových kódů na názvy (např. právní forma `112`) |

Odpověď nese vedle dat i provenanci — přesnou zdrojovou URL a čas získání:

```json
{
  "data": {"ico": "27074358", "obchodniJmeno": "Asseco Central Europe, a.s."},
  "provenance": {
    "source_id": "ares",
    "source_url": "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/27074358",
    "retrieved_at": "2026-07-28T17:41:55Z",
    "freshness": "live"
  },
  "warnings": []
}
```

## Osobní údaje

Veřejný rejstřík obsahuje údaje o lidech. Konektor s nimi zachází takto:

- `ares_subjekt_vr` vrací **jméno a funkci** statutárního orgánu — veřejný údaj,
  u kterého nástroj sám upozorňuje, že jde o osobní data;
- **datum narození ani bydliště se do modelu nikdy nepřenášejí**, i když je
  ARES v odpovědi pošle;
- ostatní nástroje osobní údaje nevracejí vůbec.

Podrobněji: [docs/COMPLIANCE.md](docs/COMPLIANCE.md).

## Vývoj

```bash
pip install -e '.[test]'
ruff check src tests
mypy src
pytest -q
openmcp-sdk validate connector.yaml
```

IČO se před dotazem ověřuje kontrolní číslicí, takže překlep nespotřebuje
volání na ARES. Cesty se skládají jen z ověřených segmentů — do URL se nikdy
nevkládá surový vstup od modelu.

## Přispívání a bezpečnost

- Postup a nároky na změny: [CONTRIBUTING.md](CONTRIBUTING.md)
- Hlášení zranitelností: [SECURITY.md](SECURITY.md) — nikdy ne přes veřejné issue
- Historie změn: [CHANGELOG.md](CHANGELOG.md)

## Licence

MIT — viz [LICENSE](LICENSE).
