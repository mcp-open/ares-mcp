"""Pin SDK v `pyproject.toml` musí souhlasit s `.sdk-ref` a vendorovaným archivem.

Tři místa drží tentýž commit SDK: závislost v `pyproject.toml` (odkud instaluje
běžný uživatel), `.sdk-ref` (podle kterého CI materializuje SDK) a název archivu
v `release/vendor/` (offline build). Kdyby se rozešly, uživatel by dostal jinou
verzi SDK než ta, proti které se konektor testoval a proti které se staví image.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEPENDENCY_RE = re.compile(
    r"openmcp-sdk\s*@\s*git\+https://github\.com/mcp-open/openmcp-sdk@([0-9a-f]{40})"
)


def _sdk_ref() -> str:
    ref = (ROOT / ".sdk-ref").read_text(encoding="utf-8").strip()
    assert SHA_RE.fullmatch(ref), f".sdk-ref není 40znakový commit SHA: {ref!r}"
    return ref


def test_pyproject_pins_the_sdk_ref_commit():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = DEPENDENCY_RE.search(pyproject)
    assert match, "pyproject.toml nemá git referenci na openmcp-sdk"
    assert match.group(1) == _sdk_ref(), (
        "pin v pyproject.toml se rozešel s .sdk-ref — uživatel by instaloval "
        "jiné SDK, než proti kterému se konektor testuje"
    )


def test_vendored_archive_matches_the_sdk_ref_commit():
    vendor = ROOT / "release" / "vendor"
    archives = sorted(vendor.glob("openmcp-sdk-*.tar.gz"))
    assert len(archives) == 1, f"čekal jsem právě jeden archiv SDK, mám {archives}"
    assert archives[0].name == f"openmcp-sdk-{_sdk_ref()}.tar.gz", (
        "vendorovaný archiv neodpovídá .sdk-ref — offline build by použil jiné SDK"
    )


def test_dependency_is_not_a_plain_pypi_requirement():
    """Jméno `openmcp-sdk` na PyPI patří cizímu projektu.

    Verzní požadavek bez URL by se z PyPI buď nevyřešil, nebo by po vydání
    cizí 0.4.x stáhl cizí kód.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in pyproject.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "openmcp-sdk" not in stripped:
            continue
        if stripped.startswith('"openmcp-sdk') or stripped.startswith("'openmcp-sdk"):
            assert "git+https://" in stripped, (
                f"závislost na openmcp-sdk musí mít git referenci: {stripped}"
            )
