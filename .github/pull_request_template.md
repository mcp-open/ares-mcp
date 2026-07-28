## Proč

<!-- Jaký problém to řeší. Ne co kód dělá — to je vidět z diffu. -->

## Kontrolní seznam

- [ ] `ruff check src tests`, `mypy src`, `pytest -q` a `openmcp-sdk validate connector.yaml` prochází
- [ ] Nový nebo změněný nástroj má `readOnlyHint`, záznam v `display.tools` a test, který ho volá
- [ ] Osobní údaje jsou deklarované v tabulkách polí, ne řešené ad hoc v kódu nástroje
- [ ] Změna manifestu, autentizace, tvaru odpovědi nebo pseudonymizačních tokenů má záznam v `CHANGELOG.md`
- [ ] V diffu nejsou tajemství, `.env` soubory ani produkční logy
- [ ] Komentáře a dokumentace jsou česky
