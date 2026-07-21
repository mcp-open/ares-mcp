# ARES no-secret referenčný connector (R1-WP06). Build context pripravuje
# platform/deploy/Makefile (target `build-connector-ares`): tar zabalí
# ares-mcp + openmcp-sdk z repos/konektory a premenuje ich na `ares/` +
# `sdk/`, lebo `ares-mcp` závisí na `openmcp-sdk` (lokálny, nie PyPI balík).
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

# `python -m connector` volá run_connector("connector.yaml", mcp)
# s relatívnou cestou — WORKDIR musí byť priečinok s connector.yaml
# (rovnaký vzor ako raynet-mcp/platform/Dockerfile; balík mcp_ares je
# nainštalovaný cez pip, importovateľný nezávisle od cwd).
WORKDIR /app/ares

EXPOSE 8000

ENTRYPOINT ["python", "-m", "connector"]
