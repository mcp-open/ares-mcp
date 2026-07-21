"""`python -m connector` — ENTRYPOINT Dockerfilu aj lokálny CLI vstup.

Identický vo všetkých konektoroch. ARES je no-secret (`credentials: []`)
a nespracúva osobné údaje mimo verejného registra, takže nepredáva ani
`test_connection`, ani `pii` — musí to sedieť s `capabilities.supports_test`
a `runtime.pii_salt` v manifeste, inak `run_connector` odmietne naštartovať.
"""

from __future__ import annotations

from openmcp_sdk import run_connector

from connector.server import mcp

run_connector("connector.yaml", mcp)
