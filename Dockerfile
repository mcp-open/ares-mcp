# ARES no-secret referenčný connector (R1-WP06). Build context pripravuje
# platform/deploy/Makefile (target `build-connector-ares`): tar zabalí
# ares-mcp + openmcp-sdk z repos/konektory a premenuje ich na `ares/` +
# `sdk/`, lebo `ares-mcp` závisí na `openmcp-sdk` (lokálny, nie PyPI balík).
FROM python:3.13-alpine@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0

WORKDIR /app

# sdk najprv (mcp-ares naň závisí v pyproject.toml).
COPY sdk ./sdk
COPY ares ./ares
RUN pip install --no-cache-dir --no-compile --only-binary=:all: \
      --require-hashes -r ./ares/release/runtime-requirements.lock \
    && pip install --no-cache-dir --no-compile --no-deps --no-build-isolation \
      ./sdk ./ares \
    && pip check

# Non-root běh s pevným UID bez domovského adresáře a login shellu.
RUN addgroup -S -g 10001 openmcp \
    && adduser -S -D -H -u 10001 -G openmcp -s /sbin/nologin openmcp
USER 10001

# `python -m connector` volá run_connector("connector.yaml", mcp)
# s relatívnou cestou — WORKDIR musí byť priečinok s connector.yaml
# (rovnaký vzor ako raynet-mcp/platform/Dockerfile; balík mcp_ares je
# nainštalovaný cez pip, importovateľný nezávisle od cwd).
WORKDIR /app/ares

EXPOSE 8000

ENTRYPOINT ["python", "-m", "connector"]
