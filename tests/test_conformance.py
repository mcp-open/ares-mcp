"""Sdílená sada testů ze SDK — kopíruje se do konektorů 1:1.

Nahrazuje to, co bylo doposud v `test_manifest.py` ručně: shodu `display.tools`
se zaregistrovanými nástroji, verzi manifest ↔ pyproject a `supports_test`
wiring.
"""

from __future__ import annotations

from openmcp_sdk.testing.conformance import ConnectorConformance


class TestConformance(ConnectorConformance):
    manifest = "connector.yaml"
    server = "connector.server:mcp"
    # ARES je no-secret a nemá `test_connection` — `/test` ověřuje credentials
    # konkrétního uživatele a ARES žádné nemá. Dostupnost upstreamu je věc
    # monitoringu platformy, ne tlačítka u konektoru.
    test_connection = None
    # Osobní údaje se nepseudonymizují: jména statutárů jsou veřejný údaj registru
    # a vracejí se s výslovným upozorněním, datum narození a bydliště se do LLM
    # nepřenášejí vůbec.
    pii_policy = None
    package = "ares-mcp"
