# Bezpečnostní politika

Nálezy **neposílejte do veřejných issue**. Pošlete je na
**security@openmcp.cz** s popisem dopadu a kroky k reprodukci. Do hlášení
nevkládejte osobní údaje ani obsah odpovědí, které je nesou.

Podporovány jsou poslední dvě minor verze konektoru.

## Co nás zajímá nejvíc

- osobní údaj, který projde do modelu, ačkoli projít neměl — u ARES jde
  konkrétně o datum narození a bydliště statutárních osob z veřejného
  rejstříku, které konektor záměrně zahazuje;
- obejití read-only filtru — konektor nemá jediný zapisující nástroj a nesmí
  ho získat ani nedopatřením;
- volání mimo `egress` allowlist v `connector.yaml`;
- cokoli, co dostane tělo upstream odpovědi do chybové zprávy pro model nebo
  do logu;
- vstup od modelu, který se dostane do URL nesestavené z ověřených segmentů.

## Co bezpečnostní chyba není

- **Data z ARES jsou veřejná.** Že konektor vrací jméno statutárního orgánu,
  je záměr, ne únik — je to veřejný údaj a nástroj `ares_subjekt_vr` na to sám
  upozorňuje.
- **Model se dá přemluvit textem z upstreamu.** To je vlastnost LLM. Konektor
  ji neřeší, jen ohraničuje dopad; u read-only konektoru bez tajemství je ten
  dopad omezený na to, co si model přečte z veřejného rejstříku.
- Neplatné IČO odmítne kontrola kontrolní číslice ještě před dotazem na ARES.
  To je záměr, ne chyba dostupnosti.
