"""Create and verify an isolated, deterministic public-history candidate.

The source repository is read only: its refs, remotes, and index are
snapshotted before the build and compared after it, and a build that changes
any of them fails. The candidate is a new one-commit Git repository containing
the current tracked and non-ignored working tree, so converged but
not-yet-committed release work is included while local caches, agent state,
credentials, and build output remain excluded by .gitignore.

The scanner half covers the complete ancestry of HEAD: every commit message,
every historical path, and every unique reachable blob, including content
deleted before HEAD. It uses read-only Git plumbing only, and findings name
the rule and the object identity; matched secret content is never captured
or printed.

Every Git subprocess runs with the caller's repository-routing and
configuration-injection variables removed, so an exported GIT_DIR or an
inherited GIT_CONFIG_COUNT cannot redirect reads or writes away from the
repository named by the explicit -C target. System and global Git
configuration are disabled for the same reason, and the built-in default
global excludes file is overridden explicitly because disabling configuration
does not disable that built-in path. One consequence is deliberate:
"non-ignored" means this repository's own ignore rules — its .gitignore files
and .git/info/exclude — never a machine-local global excludes file or an
injected core.excludesFile, so the manifest is a function of the repository's
contents and its own ignore rules rather than of the machine's Git setup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_BRANCH = "public-release"
PUBLIC_NAME = "Michael Crescenzo"
PUBLIC_EMAIL = "mcrescenzo@users.noreply.github.com"
COMMIT_DATE = "2000-01-01T00:00:00Z"
COMMIT_MESSAGE = "Initial public release candidate\n"

# Inherited variables that outrank the -C target for repository discovery, the
# work tree, the index, object storage, and the ref namespace. Git consults
# them before -C, so inheriting one lets a caller's shell redirect plumbing
# that writes (init, hash-object, update-index, update-ref) into another
# repository. Ordinary process variables such as PATH are preserved.
_GIT_ROUTING_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_SHALLOW_FILE",
    "GIT_QUARANTINE_PATH",
)

# Environment-supplied configuration (`git -c`, and what Git passes to hooks)
# is honored as if it were on the command line, so it outranks both -C and the
# NOSYSTEM/GLOBAL settings below. A caller could otherwise re-enable a global
# excludes file, or repoint core.worktree, without touching os.environ's
# routing variables at all.
_GIT_CONFIG_ENV_VARS = ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
_GIT_CONFIG_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

# Deterministic identity and date for the candidate's single root commit only.
_CANDIDATE_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": PUBLIC_NAME,
    "GIT_AUTHOR_EMAIL": PUBLIC_EMAIL,
    "GIT_COMMITTER_NAME": PUBLIC_NAME,
    "GIT_COMMITTER_EMAIL": PUBLIC_EMAIL,
    "GIT_AUTHOR_DATE": COMMIT_DATE,
    "GIT_COMMITTER_DATE": COMMIT_DATE,
}


@dataclass(frozen=True)
class SourceFile:
    path: str
    data: bytes
    mode: int


@dataclass(frozen=True)
class ScanFinding:
    rule: str
    location: str


def _stripped_env_name(name: str) -> bool:
    """True when a caller-supplied variable must not reach our Git subprocesses."""
    return (
        name in _GIT_ROUTING_ENV_VARS
        or name in _GIT_CONFIG_ENV_VARS
        or name.startswith(_GIT_CONFIG_ENV_PREFIXES)
    )


def _git_env(*, identity: bool = False) -> dict[str, str]:
    """Build a subprocess environment that keeps Git on the -C target.

    Routing and configuration-injection variables inherited from the caller are
    dropped, and system and global configuration are ignored, so only the
    explicit -C repository and this module's arguments decide what Git reads or
    writes. ``identity`` adds the fixed author, committer, and date used for
    the candidate commit.
    """
    env = {name: value for name, value in os.environ.items() if not _stripped_env_name(name)}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    if identity:
        env.update(_CANDIDATE_IDENTITY_ENV)
    return env


def _git(repo: Path, *args: str, input_bytes: bytes | None = None, env=None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=_git_env() if env is None else env,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _source_state(source: Path) -> tuple[bytes, bytes, bytes]:
    """Snapshot what candidate generation must leave alone: refs, remotes, index."""
    refs = _git(source, "for-each-ref", "--format=%(refname)%00%(objectname)")
    remotes = _git(source, "remote", "-v")
    index = _git(source, "ls-files", "--stage", "-z")
    return refs, remotes, index


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
    # Disabling system and global configuration does NOT disable Git's built-in
    # default global excludes file ($XDG_CONFIG_HOME/git/ignore, else
    # ~/.config/git/ignore), because that path is a built-in default rather
    # than a configured value. Naming it explicitly keeps the manifest
    # independent of the machine while the repository's own .gitignore files
    # and .git/info/exclude still apply.
    output = _git(
        source,
        "-c",
        f"core.excludesFile={os.devnull}",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
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


_OBJECT_RECORD = re.compile(rb"^([0-9a-f]{40,64})(?: (.*))?$")
_RAW_OID_LENGTHS = {"sha1": 20, "sha256": 32}
_BATCH_CHUNK = 256


def _object_format(repo: Path) -> str:
    fmt = _git(repo, "rev-parse", "--show-object-format").decode("ascii").strip()
    if fmt not in _RAW_OID_LENGTHS:
        raise RuntimeError(f"unsupported Git object format: {fmt}")
    return fmt


def _batch_read(
    repo: Path, oids: list[str], *, with_content: bool
) -> Iterator[tuple[str, str, int, bytes | None]]:
    """Yield (oid, kind, size, content-or-None) from read-only cat-file plumbing."""
    for start in range(0, len(oids), _BATCH_CHUNK):
        window = oids[start : start + _BATCH_CHUNK]
        request = ("\n".join(window) + "\n").encode("ascii")
        output = _git(
            repo,
            "cat-file",
            "--batch" if with_content else "--batch-check",
            input_bytes=request,
        )
        pos = 0
        for oid in window:
            newline = output.find(b"\n", pos)
            if newline < 0:
                raise RuntimeError("cat-file stream ended inside a record header")
            fields = output[pos:newline].decode("ascii").split(" ")
            if len(fields) != 3 or fields[0] != oid or not fields[2].isdigit():
                raise RuntimeError(f"cat-file returned an unusable record for {oid}")
            kind, size = fields[1], int(fields[2])
            pos = newline + 1
            content: bytes | None = None
            if with_content:
                end = pos + size
                if end + 1 > len(output):
                    raise RuntimeError(f"cat-file stream ended inside object {oid}")
                content = output[pos:end]
                pos = end + 1
            yield oid, kind, size, content


def _commit_parts(raw: bytes) -> tuple[str, bytes]:
    """Return the root tree oid and the message body of a raw commit object."""
    split = raw.find(b"\n\n")
    header = raw if split < 0 else raw[:split]
    message = b"" if split < 0 else raw[split + 2 :]
    for line in header.split(b"\n"):
        if line.startswith(b"tree "):
            return line[5:].decode("ascii"), message
    raise RuntimeError("commit object has no tree header")


def _reachable_object_oids(repo: Path) -> list[str]:
    """Every object oid reachable from HEAD, in listing order, deduplicated."""
    oids: list[str] = []
    seen: set[str] = set()
    for line in _git(repo, "rev-list", "--objects", "HEAD").split(b"\n"):
        if not line:
            continue
        match = _OBJECT_RECORD.match(line)
        if match is None:
            # Only a path containing a newline (or a foreign listing format)
            # reaches this; fail closed instead of mis-attributing history.
            raise RuntimeError("rev-list object listing produced an unparseable record")
        oid = match.group(1).decode("ascii")
        if oid not in seen:
            seen.add(oid)
            oids.append(oid)
    return oids


def _walk_trees(
    trees: dict[str, bytes], roots: list[str], raw_oid_len: int
) -> tuple[dict[str, list[str]], list[str]]:
    """Map every reachable blob to its historical paths; collect gitlink paths."""
    blob_paths: dict[str, list[str]] = {}
    gitlink_paths: list[str] = []
    seen: set[tuple[str, str]] = set()
    queue: deque[tuple[str, str]] = deque((oid, "") for oid in roots)
    while queue:
        oid, prefix = queue.popleft()
        visit = (oid, prefix)
        if visit in seen:
            continue
        seen.add(visit)
        data = trees.get(oid)
        if data is None:
            raise RuntimeError(f"reachable tree {oid} was not prefetched")
        pos = 0
        while pos < len(data):
            space = data.find(b" ", pos)
            nul = data.find(b"\0", space + 1)
            end = nul + 1 + raw_oid_len
            if space < 0 or nul < 0 or end > len(data):
                raise RuntimeError(f"tree {oid} contains a truncated entry")
            mode, name = data[pos:space], data[space + 1 : nul]
            child = data[nul + 1 : end].hex()
            child_path = prefix + name.decode("utf-8", "surrogateescape")
            if mode in (b"40000", b"040000"):
                queue.append((child, child_path + "/"))
            elif mode == b"160000":
                gitlink_paths.append(child_path)
            else:
                paths = blob_paths.setdefault(child, [])
                if child_path not in paths:
                    paths.append(child_path)
            pos = end
    return blob_paths, gitlink_paths


def _scan_history(repo: Path) -> tuple[list[ScanFinding], dict[str, int]]:
    """Scan every commit message, historical path, and unique reachable blob.

    Coverage is the full ancestry of HEAD, so content deleted before HEAD is
    still inspected. Locations identify the commit or blob object, and a path
    that itself matched a rule is never echoed back into a location string.
    """
    repo = repo.expanduser().resolve()
    raw_oid_len = _RAW_OID_LENGTHS[_object_format(repo)]
    commit_oids = _git(repo, "rev-list", "HEAD").decode("ascii").split()

    findings: list[ScanFinding] = []
    root_trees: list[str] = []
    for oid, kind, _size, content in _batch_read(repo, commit_oids, with_content=True):
        if kind != "commit" or content is None:
            raise RuntimeError(f"object {oid} is not a readable commit")
        root, message = _commit_parts(content)
        if root not in root_trees:
            root_trees.append(root)
        findings.extend(scan_bytes(message, f"commit {oid}"))

    tree_oids = [
        oid
        for oid, kind, _size, _content in _batch_read(
            repo, _reachable_object_oids(repo), with_content=False
        )
        if kind == "tree"
    ]
    trees = {
        oid: content
        for oid, kind, _size, content in _batch_read(repo, tree_oids, with_content=True)
        if kind == "tree" and content is not None
    }
    blob_paths, gitlink_paths = _walk_trees(trees, root_trees, raw_oid_len)

    flagged_paths: set[str] = set()
    all_paths: set[str] = set()
    path_hits: list[ScanFinding] = []
    for oid, paths in blob_paths.items():
        for path in paths:
            all_paths.add(path)
            hits = scan_bytes(path.encode("utf-8", "surrogateescape"), "")
            if hits:
                flagged_paths.add(path)
                path_hits.extend(
                    ScanFinding(rule=hit.rule, location=f"historical path (blob {oid})")
                    for hit in hits
                )
    for path in gitlink_paths:
        all_paths.add(path)
        path_hits.extend(
            ScanFinding(rule=hit.rule, location="historical path (gitlink)")
            for hit in scan_bytes(path.encode("utf-8", "surrogateescape"), "")
        )
    findings.extend(path_hits)

    for oid, kind, _size, content in _batch_read(repo, list(blob_paths), with_content=True):
        if kind != "blob" or content is None:
            raise RuntimeError(f"object {oid} is not a readable blob")
        paths = blob_paths[oid]
        location = (
            f"blob {oid}"
            if any(path in flagged_paths for path in paths)
            else f"blob {oid} at {paths[0]}"
        )
        findings.extend(scan_bytes(content, location))

    unique: list[ScanFinding] = []
    seen_findings: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.rule, finding.location)
        if key not in seen_findings:
            seen_findings.add(key)
            unique.append(finding)
    counts = {
        "commits": len(commit_oids),
        "historical_paths": len(all_paths),
        "blobs": len(blob_paths),
    }
    return unique, counts


def scan_history(repo: Path = REPO_ROOT) -> list[ScanFinding]:
    findings, _counts = _scan_history(repo)
    return findings


def history_scan_report(repo: Path = REPO_ROOT) -> dict[str, object]:
    findings, counts = _scan_history(repo)
    return {
        "mode": "scan-history",
        "repository": str(repo.expanduser().resolve()),
        "commits": counts["commits"],
        "historical_paths": counts["historical_paths"],
        "blobs": counts["blobs"],
        "findings": [{"rule": finding.rule, "location": finding.location} for finding in findings],
    }


def scan_candidate(candidate: Path) -> list[ScanFinding]:
    """Scan a candidate root; by construction its history is one release commit."""
    findings, _counts = _scan_history(candidate)
    return findings


def build_candidate(output: Path, source: Path = REPO_ROOT) -> dict[str, object]:
    """Write a one-commit candidate outside ``source`` and verify both sides."""
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
    # The refusal above means nothing existed at ``output`` when this call
    # started, so everything under it is this call's own work. Every failure
    # from here on — a mid-write OSError, a failed gate, KeyboardInterrupt,
    # SystemExit — removes exactly that partial candidate and re-raises
    # unchanged; otherwise the caller is left deleting a stranded artifact
    # before the next run can proceed.
    try:
        for item in manifest:
            target = output / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.data)
            target.chmod(item.mode & 0o777)

        git_env = _git_env(identity=True)
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
            object_id = _git(
                output, "hash-object", "-w", "--stdin", input_bytes=item.data, env=git_env
            )
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
        expected_identity = (
            f"{PUBLIC_NAME}\n{PUBLIC_EMAIL}\n{PUBLIC_NAME}\n{PUBLIC_EMAIL}\n\n".encode()
        )
        if identity != expected_identity:
            raise RuntimeError("candidate identity or root-parent verification failed")
        if _git(output, "remote"):
            raise RuntimeError("candidate unexpectedly has a configured remote")
        findings = scan_candidate(output)
        if findings:
            summary = ", ".join(f"{finding.rule} in {finding.location}" for finding in findings)
            raise RuntimeError(f"candidate scan failed: {summary}")
        if _source_state(source) != before:
            raise RuntimeError(
                "source repository refs, remotes, or index changed during candidate generation"
            )
    except BaseException:
        # Best-effort removal: a cleanup OSError must never mask the original
        # failure (a second interrupt delivered during cleanup still
        # propagates on its own). rmtree refuses to traverse a symlink, so
        # this can only delete files that this call created.
        shutil.rmtree(output, ignore_errors=True)
        raise

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
        "source_index_unchanged": True,
        "candidate_remotes": [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a deterministic one-commit public-history candidate outside this "
            "repository, or scan full HEAD history for prohibited content."
        )
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--output",
        type=Path,
        default=None,
        help="new output directory for the public-history candidate",
    )
    modes.add_argument(
        "--scan-history",
        action="store_true",
        help=(
            "scan every commit message, historical path, and unique reachable blob "
            "in HEAD ancestry (including content deleted before HEAD)"
        ),
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=None,
        help="repository to scan; applies only to --scan-history (default: this repository)",
    )
    args = parser.parse_args(argv)
    if args.scan_history:
        repo = args.repository if args.repository is not None else REPO_ROOT
        try:
            report = history_scan_report(repo)
        except (OSError, RuntimeError) as error:
            print(f"history scan failed: {error}", file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1 if report["findings"] else 0
    if args.repository is not None:
        parser.error("--repository applies only to --scan-history")
    try:
        report = build_candidate(args.output)
    except (OSError, RuntimeError) as error:
        print(f"public-history candidate failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
