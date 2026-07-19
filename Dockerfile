# ARES no-secret referenčný connector (R1-WP06). Build context je nadradený
# priečinok `platform/connectors/` (obsahuje `ares/` aj `sdk/`) — pozri
# `deploy/Makefile` target `build-connector-ares`, ktorý oba priečinky
# zabalí spolu, lebo `mcp-ares` závisí na `openmcp-sdk` (lokálny, nie PyPI
# balík).
FROM python:3.13-slim

WORKDIR /app

# sdk najprv (mcp-ares naň závisí v pyproject.toml).
COPY sdk ./sdk
COPY ares ./ares
RUN pip install --no-cache-dir --no-compile ./sdk ./ares

# Non-root beh — WP05 (restricted PSS baseline, ADR-022 egress mechanizmus)
# ešte nebežalo v tomto repe (žiadny commit, žiadny 65-networkpolicy.yaml).
# Toto sú rozumné defaulty analogické 85-gateway.yaml, nie oficiálny WP05
# baseline — WP05 ich smie nahradiť/zjednotiť naprieč všetkými manifestmi.
RUN useradd --uid 10001 --system --no-create-home --shell /usr/sbin/nologin openmcp
USER 10001

EXPOSE 8000

ENTRYPOINT ["python", "-m", "mcp_ares.server"]
