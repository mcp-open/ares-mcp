#!/usr/bin/env python3
"""Verify and materialize the exact vendored OpenMCP SDK snapshot.

The connector repositories must remain buildable with their repository-scoped
GitHub token. Cross-repository checkout of a private SDK is therefore not a
valid CI dependency. This helper binds the vendored archive to ``.sdk-ref`` and
its reviewed SHA-256 file, rejects unsafe tar members, and writes only regular
files into a new output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import tarfile

MAX_MEMBERS = 10_000
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class MaterializationError(RuntimeError):
    """The vendored SDK snapshot is missing, inconsistent, or unsafe."""


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_binding(root: pathlib.Path) -> tuple[str, pathlib.Path]:
    ref = (root / ".sdk-ref").read_text(encoding="utf-8").strip()
    if not SHA_RE.fullmatch(ref):
        raise MaterializationError(".sdk-ref must contain one lowercase commit SHA")

    archive = root / "release" / "vendor" / f"openmcp-sdk-{ref}.tar.gz"
    checksum_path = root / "release" / "vendor" / "openmcp-sdk.sha256"
    if not archive.is_file():
        raise MaterializationError(f"vendored SDK archive is missing: {archive.name}")

    lines = [
        line
        for line in checksum_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if len(lines) != 1 or "  " not in lines[0]:
        raise MaterializationError(
            "openmcp-sdk.sha256 must contain exactly one sha256sum record"
        )
    expected, filename = lines[0].split("  ", 1)
    if not DIGEST_RE.fullmatch(expected) or filename != archive.name:
        raise MaterializationError(
            "SDK checksum record must bind the exact .sdk-ref archive"
        )
    if _sha256(archive) != expected:
        raise MaterializationError("vendored SDK archive checksum mismatch")
    return ref, archive


def _safe_member_path(name: str) -> pathlib.PurePosixPath:
    if not name or "\\" in name or "\x00" in name:
        raise MaterializationError(f"unsafe SDK archive member: {name!r}")
    path = pathlib.PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise MaterializationError(f"unsafe SDK archive path: {name!r}")
    if path.as_posix() != name.rstrip("/"):
        raise MaterializationError(f"non-canonical SDK archive path: {name!r}")
    return path


def materialize(root: pathlib.Path, output: pathlib.Path) -> str:
    """Verify the SDK binding and materialize it into an empty directory."""

    root = root.resolve()
    output = output.resolve()
    ref, archive_path = _load_binding(root)

    if output.exists() and any(output.iterdir()):
        raise MaterializationError(f"SDK output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise MaterializationError("SDK archive member count is outside limits")

        seen: set[str] = set()
        total_size = 0
        validated: list[tuple[tarfile.TarInfo, pathlib.PurePosixPath]] = []
        for member in members:
            path = _safe_member_path(member.name)
            canonical = path.as_posix()
            if canonical in seen:
                raise MaterializationError(
                    f"duplicate SDK archive member: {canonical}"
                )
            seen.add(canonical)
            if not (member.isdir() or member.isreg()):
                raise MaterializationError(
                    f"unsupported SDK archive member type: {canonical}"
                )
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise MaterializationError(
                    f"SDK archive member exceeds size limit: {canonical}"
                )
            total_size += member.size
            if total_size > MAX_TOTAL_BYTES:
                raise MaterializationError("SDK archive exceeds total size limit")
            validated.append((member, path))

        for member, path in validated:
            target = output.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise MaterializationError(
                    f"cannot read SDK archive member: {path.as_posix()}"
                )
            with source, target.open("xb") as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)

    required = (output / "pyproject.toml", output / "openmcp_sdk" / "cli.py")
    if not all(path.is_file() for path in required):
        raise MaterializationError("SDK archive is missing required source files")
    return ref


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    ref = materialize(args.root, args.output)
    print(f"Materialized OpenMCP SDK {ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
