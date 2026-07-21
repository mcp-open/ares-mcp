"""Kontroly, ktoré chytia rozjazd medzi manifestom, kódom a build kontextom.

Tento súbor sa kopíruje do konektorov **1:1** — nič v ňom nie je špecifické
pre šablónu.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "connector.yaml").read_text(encoding="utf-8"))
SLUG = MANIFEST["slug"]


def test_version_matches_pyproject():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == MANIFEST["version"]


def test_dockerfile_directory_matches_slug():
    """Build kontext premenováva adresár repozitára na slug.

    Zdedené `COPY template ./template` teda v konektore `zasilkovna` build
    zhodí — a prejaví sa to až v CI, nie pri písaní kódu.
    """
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copied = {
        match.group(1)
        for match in re.finditer(r"^\s*COPY\s+(\S+)\s+\./\1\s*$", text, re.MULTILINE)
    }
    connector_dirs = copied - {"sdk"}
    assert connector_dirs == {SLUG}, (
        f"Dockerfile kopíruje {sorted(connector_dirs)}, ale slug je {SLUG!r}"
    )
    assert f"WORKDIR /app/{SLUG}" in text


def test_dockerfile_runs_as_expected_uid():
    """UID musí sedieť s `runAsUser: 10001` v podSecurityContext."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001" in text
    assert "--uid 10001" in text


def test_dockerignore_exists_in_repo_root():
    """V podadresári by sa neuplatnil — build kontext je nadradený priečinok."""
    assert (ROOT / ".dockerignore").is_file()


def test_sdk_ref_is_a_commit_sha():
    """CI checkoutuje SDK na tento ref; bump je jednoriadkový diff v PR."""
    ref = (ROOT / ".sdk-ref").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"nevyzerá ako commit SHA: {ref!r}"


def test_scaffold_checklist_is_removed_before_release():
    """SCAFFOLD.md smie existovať len v samotnej šablóne.

    Keď ostane v scaffoldnutom konektore, znamená to nedokončený scaffold —
    a s ním typicky aj nezmenené TODO(scaffold) placeholdery.
    """
    if SLUG == "template":
        pytest.skip("toto JE šablóna")
    assert not (ROOT / "SCAFFOLD.md").exists(), (
        "SCAFFOLD.md ostal v repozitári — scaffold nie je dokončený"
    )


#: Rozdelené, aby tento súbor nenašiel sám seba — hľadaný reťazec by inak
#: bol v ňom a test by hlásil falošný nález.
_PLACEHOLDER = "TODO" + "(scaffold)"

#: Recepty popisujú varianty, ktoré si konektor nemusí vybrať, takže v nich
#: placeholder ostáva legitímne.
_PLACEHOLDER_ALLOWED = {"tests/test_packaging.py", "docs/recipes"}


def test_no_scaffold_placeholders_left():
    if SLUG == "template":
        pytest.skip("toto JE šablóna")
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(
            part in {".git", ".venv", "node_modules", "__pycache__"}
            for part in path.parts
        ):
            continue
        if path.suffix not in {".py", ".yaml", ".yml", ".toml", ".md"}:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(rel.startswith(allowed) for allowed in _PLACEHOLDER_ALLOWED):
            continue
        if _PLACEHOLDER in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(rel)
    assert not offenders, f"nevyplnené placeholdery zo šablóny: {offenders}"


def test_egress_host_is_not_placeholder():
    """Placeholder, ktorý ticho prejde, je horší než žiadny."""
    if SLUG == "template":
        pytest.skip("toto JE šablóna")
    assert MANIFEST["egress"]["host"] != "api.example.com", (
        "egress.host je stále placeholder zo šablóny"
    )


def test_pii_connector_has_compliance_doc():
    """`runtime.pii_salt` ⇒ vyplnená COMPLIANCE.md.

    GDPR záznam podľa čl. 30 nemá kto pripomenúť. Manifest ale vie, že
    konektor spracúva osobné údaje (pýta si salt), tak si o dokument povie sám.
    """
    if not MANIFEST.get("runtime", {}).get("pii_salt"):
        pytest.skip("konektor nespracúva osobné údaje")
    doc = ROOT / "docs" / "COMPLIANCE.md"
    assert doc.is_file(), "chýba docs/COMPLIANCE.md"
    text = doc.read_text(encoding="utf-8")
    assert "čl. 30" in text, "chýba záznam o činnostiach spracovania (čl. 30)"
    if SLUG != "template":
        assert "TODO" not in text, "COMPLIANCE.md nie je vyplnená"
