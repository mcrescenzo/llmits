"""Deterministic and non-destructive public-history candidate generation."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import public_history  # noqa: E402

# Repository-routing and configuration-injection variables Git honors ahead of
# `-C`. The tool must strip them from every subprocess because some of its
# calls write, so a caller that exports one could otherwise re-init, stage
# against, or re-configure another repository.
#
# The poison sets used below are derived from the production module so these
# tests cannot drift from what the tool strips, and the pinned expectations are
# asserted against the production constants so deleting an entry there cannot
# silently weaken every assertion in this file.
EXPECTED_ROUTING_VARS = (
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
EXPECTED_CONFIG_VARS = ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")
EXPECTED_CONFIG_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")

GIT_ROUTING_VARS = public_history._GIT_ROUTING_ENV_VARS
# Git numbers injected configuration entries, and the number of entries is
# unbounded, so several indices (including a large one) are poisoned.
INJECTED_CONFIG_NAMES = EXPECTED_CONFIG_VARS + tuple(
    f"{prefix}{index}"
    for prefix in EXPECTED_CONFIG_PREFIXES
    for index in ("0", "1", "17", "999999")
)

# Invented fixture names, so a developer's own ignore rules are unlikely to
# collide with them.
MACHINE_IGNORED = "machine-ignored.txt"
REPO_IGNORED = "repo-ignored.txt"
INFO_IGNORED = "info-ignored.txt"
SOURCE_FILES = {
    "LICENSE": b"MIT License\n",
    "README.md": b"# Example\n",
    MACHINE_IGNORED: b"machine-local notes\n",
    ".gitignore": f"{REPO_IGNORED}\n".encode(),
    REPO_IGNORED: b"repository-ignored notes\n",
}


def probe_env() -> dict[str, str]:
    """Environment for this module's own Git probes, never for the tool.

    Built from the production sanitizer, so a probe cannot read from or write
    to a repository that the caller's exported ``GIT_DIR`` (or injected Git
    configuration) redirected, and the module has one definition of what
    "sanitized" means instead of one per test class.
    """
    return public_history._git_env(identity=True)


class GitProbeMixin:
    """Sanitized Git runner and repository factory for this module's own fixtures."""

    root: Path

    def git(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=probe_env(),
        ).stdout

    def init_repo(
        self,
        name: str,
        files: dict[str, bytes],
        *,
        info_exclude: str | None = None,
        track: tuple[str, ...] = ("-A",),
    ) -> Path:
        """Create a temporary repository under ``self.root`` and commit its files.

        ``track`` is passed straight to ``git add``. A fixture that depends on a
        file being *untracked* must name its tracked paths explicitly, because
        the default ``-A`` also obeys whatever ignore rules the developer's own
        machine happens to carry.
        """
        repo = self.root / name
        repo.mkdir()
        for relative, data in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git(repo, "init", "--quiet")
        if info_exclude is not None:
            (repo / ".git" / "info" / "exclude").write_text(info_exclude)
        self.git(repo, "add", *track)
        self.git(repo, "commit", "--quiet", "-m", "initial")
        return repo

    def tree_names(self, repo: Path) -> list[str]:
        return self.git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()


class PublicHistoryTests(GitProbeMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_two_candidates_from_same_tree_have_same_clean_root(self) -> None:
        source_before = public_history._source_state(REPO_ROOT)
        first_path = self.root / "first"
        second_path = self.root / "second"
        first = public_history.build_candidate(first_path)
        second = public_history.build_candidate(second_path)

        self.assertEqual(first["commit"], second["commit"])
        self.assertEqual(public_history._source_state(REPO_ROOT), source_before)
        self.assertEqual(first["scan_findings"], [])
        self.assertEqual(second["scan_findings"], [])
        self.assertEqual(self.git(first_path, "remote"), "")
        self.assertEqual(
            self.git(first_path, "rev-list", "--parents", "-n", "1", "HEAD").split(),
            [first["commit"]],
        )
        candidate_names = self.git(first_path, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        manifest_names = [item.path for item in public_history.source_manifest()]
        self.assertEqual(candidate_names, manifest_names)

        identity = self.git(first_path, "show", "-s", "--format=%an%n%ae%n%cn%n%ce", "HEAD")
        self.assertEqual(
            identity.splitlines(),
            [
                public_history.PUBLIC_NAME,
                public_history.PUBLIC_EMAIL,
                public_history.PUBLIC_NAME,
                public_history.PUBLIC_EMAIL,
            ],
        )
        self.assertEqual((first_path / "LICENSE").read_bytes(), (REPO_ROOT / "LICENSE").read_bytes())
        self.assertTrue((first_path / "tests" / "fixtures" / "zai_quota_synthetic.json").is_file())
        removed_fixture = "zai_quota_" + "live_2026-09.json"
        self.assertFalse((first_path / "tests" / "fixtures" / removed_fixture).exists())
        self.assertFalse((first_path / ".beads").exists())
        self.assertFalse((first_path / ".repo-review").exists())

    def test_scanner_detects_each_prohibited_content_class(self) -> None:
        samples = (
            b"https://" + b"claude.ai/share/example",
            b"zai_quota_" + b"live_2026-09.json",
            b"live-" + b"derived response fixture",
            b"-----BEGIN " + b"PRIVATE KEY-----",
            b"sk-" + b"abcdefghijklmnopqrstuvwxyz",
            b"ZAI_" + b"API_KEY=" + b"abcdefghijklmnop",
            b"/" + b"home/example/project",
            b"192." + b"168.10.2",
        )
        for index, sample in enumerate(samples):
            with self.subTest(index=index):
                self.assertTrue(public_history.scan_bytes(sample, "sample"))

    def test_output_must_be_new_and_outside_source(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_bytes(b"untouched\n")
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            public_history.build_candidate(existing)
        # A pre-existing directory belongs to the caller, so the refusal must
        # leave it and its contents completely alone; cleanup applies only to
        # the partial candidates this tool itself creates.
        self.assertEqual(list(existing.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"untouched\n")
        existing_file = self.root / "existing-file"
        existing_file.write_bytes(b"keep\n")
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            public_history.build_candidate(existing_file)
        self.assertEqual(existing_file.read_bytes(), b"keep\n")
        with self.assertRaisesRegex(RuntimeError, "outside the source"):
            public_history.build_candidate(REPO_ROOT / "dist" / "candidate")

    def test_late_gate_failure_cleans_partial_candidate_and_retry_succeeds(self) -> None:
        # A prohibited literal is assembled, never written whole, so this
        # test file itself can never trip the scanner's own rules.
        prohibited = b"router at " + b"192." + b"168.10.2" + b"\n"
        source = self.init_repo("source", {"LICENSE": b"MIT License\n", "notes.md": prohibited})
        output = self.root / "candidate"
        before = public_history._source_state(source)

        # scan_candidate runs only after the whole candidate repository has
        # been written, so this failure arrives at the latest possible gate.
        with self.assertRaisesRegex(RuntimeError, "candidate scan failed"):
            public_history.build_candidate(output, source)

        self.assertFalse(output.exists(), "failed build left a blocking candidate behind")
        self.assertEqual(public_history._source_state(source), before)

        self.git(source, "rm", "--quiet", "notes.md")
        self.git(source, "commit", "--quiet", "-m", "drop prohibited note")
        report = public_history.build_candidate(output, source)
        self.assertEqual(report["scan_findings"], [])
        self.assertTrue((output / "LICENSE").is_file())

    def test_mid_write_failure_cleans_partial_candidate(self) -> None:
        source = self.init_repo(
            "source", {"LICENSE": b"MIT License\n", "README.md": b"# Example\n"}
        )
        output = self.root / "candidate"
        before = public_history._source_state(source)
        real_git = public_history._git

        def fail_at_hash_object(repo, *args, input_bytes=None, env=None):
            # The output directory and its files already exist when the first
            # hash-object runs, so the failure is squarely mid-write; other
            # plumbing, including the pre-mkdir source snapshots, passes through.
            if args and args[0] == "hash-object":
                raise failure("synthetic mid-write failure")
            return real_git(repo, *args, input_bytes=input_bytes, env=env)

        # KeyboardInterrupt and SystemExit are BaseException, not Exception,
        # so every iteration proves an interrupt cannot strand a candidate.
        for failure in (OSError, KeyboardInterrupt, SystemExit):
            with self.subTest(failure=failure.__name__):
                with mock.patch.object(public_history, "_git", side_effect=fail_at_hash_object):
                    with self.assertRaises(failure) as caught:
                        public_history.build_candidate(output, source)
                # The original exception propagates with its type and message.
                self.assertEqual(str(caught.exception), "synthetic mid-write failure")
                self.assertFalse(output.exists())
                self.assertEqual(public_history._source_state(source), before)

        # The requested path is reusable with no manual cleanup.
        report = public_history.build_candidate(output, source)
        self.assertEqual(report["scan_findings"], [])

    def test_candidate_os_error_is_one_line_without_traceback(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            public_history,
            "build_candidate",
            side_effect=OSError("synthetic write failure"),
        ):
            with mock.patch.object(sys, "stderr", stderr):
                status = public_history.main(["--output", str(self.root / "candidate")])

        self.assertEqual(status, 2)
        self.assertEqual(stderr.getvalue().count("\n"), 1)
        self.assertIn(
            "public-history candidate failed: synthetic write failure",
            stderr.getvalue(),
        )
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_candidate_cli_failure_is_one_line_without_traceback(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("untouched\n")

        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "public_history.py"),
                "--output",
                str(existing),
            ],
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.count("\n"), 1)
        self.assertIn("public-history candidate failed: candidate output already exists", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(marker.read_text(), "untouched\n")


class InheritedGitEnvironmentTests(GitProbeMixin, unittest.TestCase):
    """An exported Git routing variable must not redirect the tool's plumbing.

    Every Git call names its repository with `-C`, and the candidate calls
    write, so inherited routing variables are a correctness and safety issue.
    All repositories here are temporary; the tool's own assertions are what
    must fail, never a maintainer's checkout.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_git_environment_strips_repository_routing_variables(self) -> None:
        names = GIT_ROUTING_VARS + INJECTED_CONFIG_NAMES
        poison = {name: str(self.root / "poison") for name in names}

        with mock.patch.dict(os.environ, poison):
            env = public_history._git_env()

        # Compare key sets rather than asserting membership against `env`: a
        # failing assertion would print the whole subprocess environment, and
        # that environment is the developer's real shell.
        self.assertEqual(set(names) & set(env), set())
        # The module's own configuration overrides survive the strip.
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        # Identity is added only for candidate writes, so a plain environment
        # passes the caller's own values through untouched.
        for name in public_history._CANDIDATE_IDENTITY_ENV:
            self.assertEqual(env.get(name), os.environ.get(name), name)

    def test_candidate_git_environment_is_sanitized_too(self) -> None:
        names = GIT_ROUTING_VARS + INJECTED_CONFIG_NAMES
        poison = {name: str(self.root / "poison") for name in names}

        with mock.patch.dict(os.environ, poison):
            env = public_history._git_env(identity=True)

        self.assertEqual(set(names) & set(env), set())
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_AUTHOR_NAME"], public_history.PUBLIC_NAME)
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], public_history.PUBLIC_EMAIL)
        self.assertEqual(env["GIT_AUTHOR_DATE"], public_history.COMMIT_DATE)
        self.assertEqual(env["GIT_COMMITTER_DATE"], public_history.COMMIT_DATE)

    def test_candidate_generation_ignores_an_exported_git_dir(self) -> None:
        source = self.init_repo(
            "source", {"LICENSE": b"MIT License\n", "README.md": b"# Example\n"}
        )
        decoy = self.init_repo("decoy", {"decoy.txt": b"decoy content\n"})
        source_state = public_history._source_state(source)
        decoy_state = public_history._source_state(decoy)
        candidate = self.root / "candidate"

        with mock.patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git")}):
            report = public_history.build_candidate(candidate, source)

        self.assertEqual(public_history._source_state(source), source_state)
        self.assertEqual(public_history._source_state(decoy), decoy_state)
        self.assertEqual(self.git(decoy, "status", "--porcelain"), "")
        self.assertTrue((candidate / ".git").is_dir())
        self.assertEqual(
            self.git(candidate, "ls-tree", "-r", "--name-only", "HEAD").splitlines(),
            ["LICENSE", "README.md"],
        )
        self.assertEqual(Path(report["candidate"]).resolve(), candidate.resolve())
        self.assertEqual(report["scan_findings"], [])

    def test_source_state_notices_an_index_change(self) -> None:
        source = self.init_repo("source", {"LICENSE": b"MIT License\n"})
        before = public_history._source_state(source)

        (source / "staged.txt").write_bytes(b"staged content\n")
        self.git(source, "update-index", "--add", "staged.txt")

        self.assertNotEqual(public_history._source_state(source), before)

    def test_candidate_report_confirms_the_source_index_is_unchanged(self) -> None:
        source = self.init_repo("source", {"LICENSE": b"MIT License\n"})
        before = public_history._source_state(source)

        report = public_history.build_candidate(self.root / "candidate", source)

        self.assertIs(report["source_index_unchanged"], True)
        self.assertEqual(public_history._source_state(source), before)

    def test_history_scan_stays_on_the_requested_repository(self) -> None:
        token = "sk-" + "routingprobe01234"
        target = self.init_repo("target", {"notes.txt": f"api_key={token}\n".encode()})
        decoy = self.init_repo("decoy", {"decoy.txt": b"ordinary content\n"})
        env = dict(os.environ)
        env["GIT_DIR"] = str(decoy / ".git")

        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "public_history.py"),
                "--scan-history",
                "--repository",
                str(target),
            ],
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("high-confidence access token", completed.stdout)
        self.assertNotIn(token, completed.stdout)
        self.assertEqual(self.git(decoy, "status", "--porcelain"), "")


class SanitizerSpecTests(unittest.TestCase):
    """The production strip set is the contract these tests poison, so pin it."""

    def test_production_sanitizer_matches_the_pinned_expectation(self) -> None:
        missing = set(EXPECTED_ROUTING_VARS) - set(public_history._GIT_ROUTING_ENV_VARS)
        self.assertEqual(missing, set(), "production stopped stripping a routing variable")
        self.assertEqual(tuple(public_history._GIT_CONFIG_ENV_VARS), EXPECTED_CONFIG_VARS)
        self.assertEqual(tuple(public_history._GIT_CONFIG_ENV_PREFIXES), EXPECTED_CONFIG_PREFIXES)

    def test_stripped_name_predicate_covers_dynamic_and_preserved_names(self) -> None:
        for name in GIT_ROUTING_VARS + INJECTED_CONFIG_NAMES:
            self.assertTrue(public_history._stripped_env_name(name), name)
        for name in ("PATH", "HOME", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_AUTHOR_NAME"):
            self.assertFalse(public_history._stripped_env_name(name), name)


class CandidateIgnoreRuleTests(GitProbeMixin, unittest.TestCase):
    """The candidate's file set must follow repository-owned ignore rules only.

    Everything here is invented data in temporary repositories; the real
    checkout is never a fixture.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    @staticmethod
    def machine_home(root: Path) -> Path:
        """A temporary HOME whose built-in global excludes file hides a fixture."""
        home = root / "machine-home"
        (home / "git").mkdir(parents=True)
        (home / "git" / "ignore").write_text(f"{MACHINE_IGNORED}\n")
        return home

    # ``-f`` keeps the fixture's tracked set independent of the developer's own
    # ignore rules; the files whose exclusion is under test stay untracked.
    TRACKED_FILES = ("-f", "LICENSE", "README.md", ".gitignore")

    def test_machine_level_ignores_never_change_the_candidate(self) -> None:
        source = self.init_repo("source", dict(SOURCE_FILES), track=self.TRACKED_FILES)
        reference_path = self.root / "reference"
        reference = public_history.build_candidate(reference_path, source)
        reference_names = self.tree_names(reference_path)
        # Fixture preconditions: the file a machine-level ignore hides must be
        # untracked, or no exclude rule could ever apply to it, and the
        # repository's own ignore file must already be in effect.
        self.assertIn(MACHINE_IGNORED, reference_names)
        self.assertNotIn(REPO_IGNORED, reference_names)
        home = self.machine_home(self.root)

        poisoned = (
            # Git's built-in default global excludes file: disabling system and
            # global configuration does not disable that built-in path.
            {"HOME": str(home), "XDG_CONFIG_HOME": str(home)},
            # Configuration injected through the environment, as a git hook or
            # an embedding process does.
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.excludesFile",
                "GIT_CONFIG_VALUE_0": str(home / "git" / "ignore"),
            },
            {"GIT_CONFIG_PARAMETERS": f"'core.excludesFile={home / 'git' / 'ignore'}'"},
        )
        for index, poison in enumerate(poisoned):
            with self.subTest(poison=index), mock.patch.dict(os.environ, poison):
                output = self.root / f"poisoned-{index}"
                report = public_history.build_candidate(output, source)
                self.assertEqual(report["commit"], reference["commit"], poison)
                self.assertEqual(self.tree_names(output), reference_names, poison)

    def test_repository_owned_ignore_rules_still_apply(self) -> None:
        source = self.init_repo(
            "source",
            dict(SOURCE_FILES),
            info_exclude=f"{INFO_IGNORED}\n",
            track=self.TRACKED_FILES,
        )
        (source / INFO_IGNORED).write_bytes(b"repository-local exclude notes\n")
        output = self.root / "candidate"

        public_history.build_candidate(output, source)

        names = self.tree_names(output)
        self.assertIn("LICENSE", names)
        self.assertNotIn(REPO_IGNORED, names, ".gitignore stopped applying")
        self.assertNotIn(INFO_IGNORED, names, ".git/info/exclude stopped applying")


class ProbeHelperIsolationTests(GitProbeMixin, unittest.TestCase):
    """This module's own temporary Git operations must stay on their repository."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_module_probe_helpers_ignore_a_redirected_git_environment(self) -> None:
        decoy = self.root / "decoy"
        decoy.mkdir()
        poison = {
            "GIT_DIR": str(decoy / ".git"),
            "GIT_WORK_TREE": str(decoy),
            "GIT_INDEX_FILE": str(decoy / "index"),
            "GIT_OBJECT_DIRECTORY": str(decoy / "objects"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": str(decoy),
            "GIT_CONFIG_PARAMETERS": f"'core.worktree={decoy}'",
        }

        with mock.patch.dict(os.environ, poison):
            repo = self.init_repo("probe", {"LICENSE": b"MIT License\n"})
            head = self.git(repo, "rev-parse", "HEAD").strip()
            status = self.git(repo, "status", "--porcelain")

        self.assertEqual(list(decoy.iterdir()), [], "a probe wrote into the redirected repository")
        self.assertEqual(status, "")
        self.assertEqual(len(head), 40)
        self.assertEqual(self.tree_names(repo), ["LICENSE"])


class HistoryScanTests(GitProbeMixin, unittest.TestCase):
    """Full-ancestry scanner coverage, including content deleted before HEAD."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "--quiet")

    def write(self, relative: str, data: bytes) -> None:
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def head(self) -> str:
        return self.git(self.repo, "rev-parse", "HEAD").strip()

    def commit(self, message: str) -> str:
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "--quiet", "-m", message)
        return self.head()

    def run_scanner(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "public_history.py"),
                "--scan-history",
                "--repository",
                str(self.repo),
            ],
            capture_output=True,
            text=True,
        )

    def test_secret_deleted_before_head_is_still_found(self) -> None:
        token = b"sk-" + b"ancestorprobe0123"
        self.write("settings.txt", b"api_key=" + token + b"\n")
        self.commit("add local settings")
        self.git(self.repo, "rm", "--quiet", "settings.txt")
        self.commit("remove local settings")
        # The prohibited value exists only in an ancestor, not in the HEAD tree,
        # so a HEAD-only scan would pass and this test would fail.
        self.assertNotIn(
            "settings.txt", self.git(self.repo, "ls-tree", "-r", "--name-only", "HEAD")
        )

        report = public_history.history_scan_report(self.repo)
        self.assertEqual(
            [finding["rule"] for finding in report["findings"]],
            ["high-confidence access token"],
        )
        self.assertRegex(
            report["findings"][0]["location"], r"^blob [0-9a-f]{40}( at settings\.txt)?$"
        )
        self.assertNotIn(token.decode(), json.dumps(report))

        completed = self.run_scanner()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("high-confidence access token", completed.stdout)
        self.assertNotIn(token.decode(), completed.stdout)

    def test_clean_full_history_with_deletions_passes(self) -> None:
        self.write("keep.txt", b"ordinary content\n")
        self.commit("first")
        self.write("nested/deep.txt", b"more ordinary content\n")
        self.commit("second")
        self.git(self.repo, "rm", "--quiet", "keep.txt")
        self.commit("third")

        report = public_history.history_scan_report(self.repo)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["commits"], 3)
        completed = self.run_scanner()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["findings"], [])

    def test_commit_message_content_is_scanned(self) -> None:
        url = (b"https://" + b"claude.ai/share/" + b"note").decode()
        self.write("doc.txt", b"clean\n")
        oid = self.commit(f"see {url} for context")
        report = public_history.history_scan_report(self.repo)
        self.assertEqual(
            report["findings"],
            [{"rule": "Claude session URL", "location": f"commit {oid}"}],
        )

    def test_prohibited_path_name_is_found_without_being_echoed(self) -> None:
        fixture_name = "zai_quota_" + "live_2026-09.json"
        self.write(fixture_name, b"{}\n")
        self.commit("add fixture")
        report = public_history.history_scan_report(self.repo)
        rules = {finding["rule"] for finding in report["findings"]}
        self.assertIn("removed account fixture filename", rules)
        serialized = json.dumps(report)
        self.assertNotIn(fixture_name, serialized)
        self.assertNotIn("live_2026-09", serialized)

    def test_unique_blob_is_scanned_once_across_paths(self) -> None:
        payload = b"sk-" + b"sharedblobprobe12"
        self.write("one.txt", payload)
        self.commit("first copy")
        self.write("two.txt", payload)
        self.commit("second copy")
        report = public_history.history_scan_report(self.repo)
        token_findings = [
            finding
            for finding in report["findings"]
            if finding["rule"] == "high-confidence access token"
        ]
        self.assertEqual(len(token_findings), 1)
        self.assertEqual(report["blobs"], 1)
        self.assertEqual(report["historical_paths"], 2)
        self.assertEqual(report["commits"], 2)

    def test_moved_unchanged_subtree_preserves_every_historical_path(self) -> None:
        old_directory = "zai_quota_" + "live_2026-09"
        self.write(f"{old_directory}/payload.txt", b"ordinary content\n")
        self.commit("add synthetic tree")
        self.git(self.repo, "mv", old_directory, "clean")
        self.commit("move synthetic tree")
        self.assertNotIn(
            old_directory,
            self.git(self.repo, "ls-tree", "-r", "--name-only", "HEAD"),
        )

        report = public_history.history_scan_report(self.repo)
        self.assertIn(
            "removed account fixture filename",
            {finding["rule"] for finding in report["findings"]},
        )
        self.assertEqual(report["historical_paths"], 2)
        self.assertNotIn(old_directory, json.dumps(report))

    def test_scanner_cli_modes_are_mutually_exclusive(self) -> None:
        script = str(REPO_ROOT / "tools" / "public_history.py")
        both = subprocess.run(
            [sys.executable, script, "--output", str(self.root / "out"), "--scan-history"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(both.returncode, 2)
        self.assertFalse((self.root / "out").exists())
        misdirected = subprocess.run(
            [
                sys.executable,
                script,
                "--output",
                str(self.root / "out"),
                "--repository",
                str(self.repo),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(misdirected.returncode, 2)

    def test_public_repository_history_is_clean(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "public_history.py"), "--scan-history"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["findings"], [])


if __name__ == "__main__":
    unittest.main()
