"""Spoločná sada testov zo SDK — kopíruje sa do konektorov 1:1.

Nahrádza to, čo bolo doteraz v `test_manifest.py` ručne: zhodu `display.tools`
so zaregistrovanými nástrojmi, verziu manifest ↔ pyproject a `supports_test`
wiring.
"""

from __future__ import annotations

from openmcp_sdk.testing.conformance import ConnectorConformance


class TestConformance(ConnectorConformance):
    manifest = "connector.yaml"
    server = "connector.server:mcp"
    # ARES je no-secret a nemá `test_connection` — `/test` overuje credentials
    # konkrétneho používateľa a ARES žiadne nemá. Dostupnosť upstreamu je vec
    # monitoringu platformy, nie tlačidla pri konektore.
    test_connection = None
    # Osobné údaje sa nepseudonymizujú: mená štatutárov sú verejný údaj registra
    # a vracajú sa s výslovným upozornením, dátum narodenia a bydlisko sa do LLM
    # neprenášajú vôbec.
    pii_policy = None
    package = "ares-mcp"
