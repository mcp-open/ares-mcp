"""`python -m connector` — ENTRYPOINT Dockerfilu i lokální CLI vstup.

Identický ve všech konektorech. ARES je no-secret (`credentials: []`) a
nepseudonymizuje osobní údaje, takže nepředává ani `test_connection`, ani
`pii`. Veřejná jména statutárů přesto zůstávají osobními údaji; jejich
minimalizaci a compliance hranici popisuje ``docs/COMPLIANCE.md``. Runtime
argumenty musí sedět s `capabilities.supports_test` a `runtime.pii_salt`
v manifestu, jinak `run_connector` odmítne nastartovat.
"""

from __future__ import annotations

from openmcp_sdk import run_connector

from connector.server import mcp, public_safe_test

run_connector("connector.yaml", mcp, public_safe_test=public_safe_test)
