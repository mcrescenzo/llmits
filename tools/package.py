"""Build the deterministic, licensed llmits zipapp from a clean staging tree.

Only reviewed first-party source is packaged: a copy of src/llmits with
__pycache__ and .pyc artifacts removed, plus the repository's LICENSE as
``llmits/LICENSE``. Archive bytes are fully determined by the staged
content: fixed entry timestamps, fixed Unix file modes, a fixed Unix
create_system, a path-sorted entry order, and ``__main__.py`` written
first — never the build clock, timezone, locale, filesystem ordering, or
umask. Two builds from identical source produce byte-identical artifacts.
Used by `make build` and by the packaging-manifest test.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PACKAGE = REPO_ROOT / "src" / "llmits"
LICENSE_FILE = REPO_ROOT / "LICENSE"
INTERPRETER = "/usr/bin/env python3"

# The ZIP epoch minimum (1980-01-01 00:00:00): the conventional fixed
# timestamp for reproducible archives.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
# Unix "created on Unix" marker, so entries never depend on the host OS.
UNIX_CREATE_SYSTEM = 3
FIXED_FILE_MODE = 0o644
ENTRYPOINT_MODE = 0o755


def build(output: Path, source: Path = SOURCE_PACKAGE) -> Path:
    if not (source / "__init__.py").is_file():
        raise SystemExit(f"package source not found: {source}")
    if not LICENSE_FILE.is_file():
        raise SystemExit(f"license file not found: {LICENSE_FILE}")

    # Derived from source.name (not a fixed module-level constant) so a
    # caller that overrides `source` still gets an entry point matching the
    # directory name staged and copied below, instead of silently going out
    # of sync with it.
    entrypoint = f"{source.name}.cli:run"
    with tempfile.TemporaryDirectory(prefix="llimits-build-") as tmp:
        staging = Path(tmp) / source.name
        shutil.copytree(source, staging)
        for cache in staging.rglob("__pycache__"):
            shutil.rmtree(cache)
        for pyc in staging.rglob("*.pyc"):
            pyc.unlink()
        shutil.copyfile(LICENSE_FILE, staging / "LICENSE")
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_archive(output, staging, entrypoint)
    # Normalize the artifact so it is always executable by the owner and
    # never group/world-writable, regardless of the process umask.
    output.chmod(0o755)
    return output


def _write_entry(archive: zipfile.ZipFile, arcname: str, data: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
    info.create_system = UNIX_CREATE_SYSTEM
    info.external_attr = (mode & 0xFFFF) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def _write_archive(output: Path, staging: Path, entrypoint: str) -> None:
    """Write the shebang-prefixed archive with fully normalized metadata."""
    module = staging.name
    main_py = f"import {entrypoint.split(':')[0]}\n{entrypoint.replace(':', '.')}()\n".encode()
    entries = sorted(
        (f"{module}/{path.relative_to(staging).as_posix()}", path)
        for path in staging.rglob("*")
        if path.is_file()
    )
    # open(..., "wb") is the one deliberate write in the build tool; the
    # src/ tree itself stays strictly read-only (enforced by tests).
    with open(output, "wb") as artifact:
        artifact.write(f"#!{INTERPRETER}\n".encode("ascii"))
        with zipfile.ZipFile(artifact, "w") as archive:
            _write_entry(archive, "__main__.py", main_py, ENTRYPOINT_MODE)
            for arcname, path in entries:
                _write_entry(archive, arcname, path.read_bytes(), FIXED_FILE_MODE)


def nondeterministic_entries(archive: zipfile.ZipFile) -> list[str]:
    """Return entry names (or "(entry order)") violating the reproducibility contract.

    Reusable by CI and final verification: an empty result means every entry
    carries the fixed timestamp, Unix create_system, and normalized mode, and
    ``__main__.py`` precedes the path-sorted package entries.
    """
    offenders: list[str] = []
    names = archive.namelist()
    if names != ["__main__.py"] + sorted(names[1:]):
        offenders.append("(entry order)")
    for info in archive.infolist():
        expected_mode = ENTRYPOINT_MODE if info.filename == "__main__.py" else FIXED_FILE_MODE
        if (
            info.date_time != FIXED_DATE_TIME
            or info.create_system != UNIX_CREATE_SYSTEM
            or ((info.external_attr >> 16) & 0xFFFF) != expected_mode
        ):
            offenders.append(info.filename)
    return offenders


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the llmits zipapp.")
    parser.add_argument("--output", default="dist/llmits", help="output path")
    args = parser.parse_args(argv)
    artifact = build(Path(args.output))
    print(f"built {artifact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
