"""Kontroly, které zachytí rozjezd mezi manifestem, kódem a build kontextem.

Tento soubor se kopíruje do konektorů **1:1** — nic v něm není specifické
pro šablonu.
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
    """Build kontext přejmenovává adresář repozitáře na slug.

    Zděděné `COPY template ./template` tedy v konektoru `zasilkovna` build
    shodí — a projeví se to až v CI, ne při psaní kódu.
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
    """UID musí sedět s `runAsUser: 10001` v podSecurityContext."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER 10001" in text
    assert re.search(r"\badduser\b[^\n]*\s-u\s+10001\b", text)


def test_dockerignore_exists_in_repo_root():
    """V podadresáři by se neuplatnil — build kontext je nadřazená složka."""
    assert (ROOT / ".dockerignore").is_file()


def test_sdk_ref_is_a_commit_sha():
    """CI checkoutuje SDK na tento ref; bump je jednořádkový diff v PR."""
    ref = (ROOT / ".sdk-ref").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"nevypadá jako commit SHA: {ref!r}"


def test_scaffold_checklist_is_removed_before_release():
    """SCAFFOLD.md smí existovat jen v samotné šabloně.

    Když zůstane ve scaffoldnutém konektoru, znamená to nedokončený scaffold —
    a s ním typicky i nezměněné TODO(scaffold) placeholdery.
    """
    if SLUG == "template":
        pytest.skip("toto JE šablona")
    assert not (ROOT / "SCAFFOLD.md").exists(), (
        "SCAFFOLD.md zůstal v repozitáři — scaffold není dokončen"
    )


#: Rozdělené, aby tento soubor nenašel sám sebe — hledaný řetězec by jinak
#: byl v něm a test by hlásil falešný nález.
_PLACEHOLDER = "TODO" + "(scaffold)"

#: Recepty popisují varianty, které si konektor nemusí vybrat, takže v nich
#: placeholder zůstává legitimní.
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
    assert not offenders, f"nevyplněné placeholdery ze šablony: {offenders}"


def test_egress_host_is_not_placeholder():
    """Placeholder, který tiše projde, je horší než žádný."""
    if SLUG == "template":
        pytest.skip("toto JE šablona")
    assert MANIFEST["egress"]["host"] != "api.example.com", (
        "egress.host je stále placeholder ze šablony"
    )


def test_pii_connector_has_compliance_doc():
    """PII salt nebo explicitní osobní data ⇒ vyplněná COMPLIANCE.md.

    ``runtime.pii_salt`` rozpozná pseudonymizující konektory, ale není úplným
    klasifikátorem osobních údajů. ARES například záměrně vrací veřejná jména
    statutárů bez pseudonymizace a tuto skutečnost deklaruje v katalogovém
    ``display.data_handling``. I takový konektor musí mít compliance záznam.
    """
    data_handling = str(MANIFEST.get("display", {}).get("data_handling", "")).casefold()
    declares_personal_data = any(
        marker in data_handling
        for marker in ("osobní", "jména", "fyzických osob", "pii")
    )
    if not MANIFEST.get("runtime", {}).get("pii_salt") and not declares_personal_data:
        pytest.skip("manifest nedeklaruje pseudonymizaci ani osobní údaje")
    doc = ROOT / "docs" / "COMPLIANCE.md"
    assert doc.is_file(), "chybí docs/COMPLIANCE.md"
    text = doc.read_text(encoding="utf-8")
    assert "čl. 30" in text, "chybí záznam o činnostech zpracování (čl. 30)"
    assert "veřejného rejstříku" in text
    assert "datum narození" in text
    assert "bydliště" in text
    if SLUG != "template":
        assert "TODO" not in text, "COMPLIANCE.md není vyplněná"
