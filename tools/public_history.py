"""Create and verify an isolated, deterministic public-history candidate.

The source repository is read only. The candidate is a new one-commit Git
repository containing the current tracked and non-ignored working tree, so
converged but not-yet-committed release work is included while local caches,
agent state, credentials, and build output remain excluded by .gitignore.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_BRANCH = "public-release"
PUBLIC_NAME = "Michael Crescenzo"
PUBLIC_EMAIL = "mcrescenzo@users.noreply.github.com"
COMMIT_DATE = "2000-01-01T00:00:00Z"
COMMIT_MESSAGE = "Initial public release candidate\n"


@dataclass(frozen=True)
class SourceFile:
    path: str
    data: bytes
    mode: int


@dataclass(frozen=True)
class ScanFinding:
    rule: str
    location: str


def _git(repo: Path, *args: str, input_bytes: bytes | None = None, env=None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _source_state(source: Path) -> tuple[bytes, bytes]:
    refs = _git(source, "for-each-ref", "--format=%(refname)%00%(objectname)")
    remotes = _git(source, "remote", "-v")
    return refs, remotes


def _tracked_modes(source: Path) -> dict[str, int]:
    modes: dict[str, int] = {}
    output = _git(source, "ls-files", "--stage", "-z")
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        raw_mode, _object_id, stage = metadata.split()
        if stage == b"0":
            modes[raw_path.decode("utf-8", "surrogateescape")] = int(raw_mode, 8)
    return modes


def source_manifest(source: Path = REPO_ROOT) -> tuple[SourceFile, ...]:
    source = source.resolve()
    tracked_modes = _tracked_modes(source)
    output = _git(source, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    files: list[SourceFile] = []
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", "surrogateescape")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or ".git" in pure.parts:
            raise RuntimeError(f"unsafe source path: {relative!r}")
        path = source / relative
        if not path.exists():
            # A tracked deletion belongs in the converged tree as an absence.
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"public candidate supports regular files only: {relative!r}")
        indexed_mode = tracked_modes.get(relative)
        executable = (
            indexed_mode == 0o100755
            if indexed_mode is not None
            else bool(path.stat().st_mode & stat.S_IXUSR)
        )
        files.append(SourceFile(relative, path.read_bytes(), 0o100755 if executable else 0o100644))
    files.sort(key=lambda item: item.path.encode("utf-8", "surrogateescape"))
    if not any(item.path == "LICENSE" for item in files):
        raise RuntimeError("refusing to create an unlicensed public-history root")
    return tuple(files)


def _scan_rules() -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    api_names = b"(?:ZAI_" + b"API_KEY|ZHIPU_" + b"API_KEY|ANTHROPIC_" + b"API_KEY|OPENAI_" + b"API_KEY)"
    return (
        (
            "Claude session URL",
            re.compile(rb"https?://" + rb"(?:www\.)?" + rb"claude\.ai/\S+", re.IGNORECASE),
        ),
        (
            "removed account fixture filename",
            re.compile(b"zai_quota_" + b"live_2026-09", re.IGNORECASE),
        ),
        (
            "account-derived fixture wording",
            re.compile(rb"live[- ](?:derived|account|fixture)", re.IGNORECASE),
        ),
        (
            "private key material",
            re.compile(b"-----BEGIN " + rb"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        ),
        (
            "high-confidence access token",
            re.compile(
                rb"(?:sk-(?!sentinel-)(?:ant-|proj-)?|ghp_|github_pat_|AIza)[A-Za-z0-9_-]{16,}"
            ),
        ),
        (
            "assigned provider credential",
            re.compile(api_names + rb"\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}"),
        ),
        (
            "absolute user-home path",
            re.compile(rb"/(?:home|Users)/[A-Za-z0-9._-]+/"),
        ),
        (
            "private IPv4 literal",
            re.compile(
                rb"(?<![0-9])(?:10(?:\.[0-9]{1,3}){3}|192\.168(?:\.[0-9]{1,3}){2}|"
                rb"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2})(?![0-9])"
            ),
        ),
    )


def scan_bytes(data: bytes, location: str) -> list[ScanFinding]:
    return [
        ScanFinding(rule=label, location=location)
        for label, pattern in _scan_rules()
        if pattern.search(data)
    ]


def scan_candidate(candidate: Path) -> list[ScanFinding]:
    findings = scan_bytes(_git(candidate, "log", "--format=%B"), "commit messages")
    names = _git(candidate, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    for raw_name in names:
        name = raw_name.decode("utf-8", "surrogateescape")
        blob = _git(candidate, "show", f"HEAD:{name}")
        findings.extend(scan_bytes(raw_name + b"\0" + blob, name))
    return findings


def _isolated_git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": PUBLIC_NAME,
            "GIT_AUTHOR_EMAIL": PUBLIC_EMAIL,
            "GIT_COMMITTER_NAME": PUBLIC_NAME,
            "GIT_COMMITTER_EMAIL": PUBLIC_EMAIL,
            "GIT_AUTHOR_DATE": COMMIT_DATE,
            "GIT_COMMITTER_DATE": COMMIT_DATE,
        }
    )
    return env


def build_candidate(output: Path, source: Path = REPO_ROOT) -> dict[str, object]:
    source = source.resolve()
    output = output.expanduser().resolve()
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise RuntimeError("candidate output must be outside the source repository")
    if output.exists():
        raise RuntimeError(f"candidate output already exists: {output}")

    before = _source_state(source)
    manifest = source_manifest(source)
    output.mkdir(parents=True, mode=0o700)
    for item in manifest:
        target = output / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.data)
        target.chmod(item.mode & 0o777)

    git_env = _isolated_git_env()
    _git(
        output,
        "init",
        "--quiet",
        f"--initial-branch={PUBLIC_BRANCH}",
        "--object-format=sha1",
        "--template=",
        env=git_env,
    )
    for item in manifest:
        object_id = _git(output, "hash-object", "-w", "--stdin", input_bytes=item.data, env=git_env)
        object_id_text = object_id.decode("ascii").strip()
        _git(
            output,
            "update-index",
            "--add",
            "--cacheinfo",
            f"{item.mode:o},{object_id_text},{item.path}",
            env=git_env,
        )
    tree = _git(output, "write-tree", env=git_env).decode("ascii").strip()
    commit = _git(
        output,
        "-c",
        "commit.gpgSign=false",
        "commit-tree",
        tree,
        input_bytes=COMMIT_MESSAGE.encode("utf-8"),
        env=git_env,
    ).decode("ascii").strip()
    _git(output, "update-ref", f"refs/heads/{PUBLIC_BRANCH}", commit, env=git_env)
    _git(output, "symbolic-ref", "HEAD", f"refs/heads/{PUBLIC_BRANCH}", env=git_env)

    identity = _git(output, "show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%P", "HEAD")
    expected_identity = f"{PUBLIC_NAME}\n{PUBLIC_EMAIL}\n{PUBLIC_NAME}\n{PUBLIC_EMAIL}\n\n".encode()
    if identity != expected_identity:
        raise RuntimeError("candidate identity or root-parent verification failed")
    if _git(output, "remote"):
        raise RuntimeError("candidate unexpectedly has a configured remote")
    findings = scan_candidate(output)
    if findings:
        summary = ", ".join(f"{finding.rule} in {finding.location}" for finding in findings)
        raise RuntimeError(f"candidate scan failed: {summary}")
    if _source_state(source) != before:
        raise RuntimeError("source repository refs or remotes changed during candidate generation")

    license_entry = next(item for item in manifest if item.path == "LICENSE")
    return {
        "candidate": str(output),
        "commit": commit,
        "branch": PUBLIC_BRANCH,
        "author_email": PUBLIC_EMAIL,
        "file_count": len(manifest),
        "license_sha256": hashlib.sha256(license_entry.data).hexdigest(),
        "scan_findings": [],
        "source_refs_unchanged": True,
        "source_remotes_unchanged": True,
        "candidate_remotes": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a deterministic one-commit public-history candidate outside this repository."
    )
    parser.add_argument("--output", required=True, type=Path, help="new output directory")
    args = parser.parse_args(argv)
    report = build_candidate(args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
