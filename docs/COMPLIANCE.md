# ARES connector: záznam zpracování veřejných osobních údajů

Tento dokument je technický podklad pro záznam činností zpracování podle
čl. 30 GDPR. Nenahrazuje právní posouzení konkrétního provozovatele OpenMCP.

## Rozsah a účel

Konektor pouze čte veřejné české registry. Nástroj `ares_subjekt_vr` může pro
ověření zastupování právnické osoby vrátit jméno a funkci aktuálního člena
statutárního orgánu. Jméno fyzické osoby z veřejného rejstříku je osobní údaj,
i když je zdroj veřejný.

Konektor záměrně nepřenáší datum narození, bydliště, historicky vymazané členy
ani další identitní atributy statutárů. Nástroje RŽP a NRPZS zahazují osoby,
které nejsou nutné pro deklarovaný firemní nebo institucionální účel.

## Zdroj, příjemci a uložení

- Zdroj: veřejné API ARES provozované českou veřejnou správou.
- Příjemce: oprávněný uživatel a jím zvolený AI klient v rámci konkrétního MCP
  volání.
- Uložení konektorem: žádné; konektor je bezstavový a provider odpovědi
  neukladá.
- Logy: provider payload ani osobní údaje se nesmějí zapisovat do aplikačních
  logů.

## Technické ochrany

- výstup VR je redukovaný na aktuální jméno, funkci a orgán;
- datum narození a bydliště jsou ve schématu zakázané a pokryté regresními
  testy;
- každý VR výsledek nese výslovné upozornění na osobní údaje;
- konektor je pouze pro čtení, bez uživatelských credentials a bez
  serverového session stavu;
- síťový výstup je omezen na deklarovaný HTTPS provider.

Provozovatel musí před zpřístupněním uživatelům určit právní titul, dobu
uchování na navazujících platformních vrstvách a příslušné informační
povinnosti. Veřejnost zdroje sama o sobě tyto povinnosti neruší.
