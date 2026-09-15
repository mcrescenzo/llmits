# Contributing to llmits

Contributions are welcome. Keep changes small enough to review and preserve the security boundaries described in [SECURITY.md](SECURITY.md).

## Development setup

You need Linux, Make, and Python 3.11 or newer. The application and test suite have no third-party runtime dependencies. Ruff and mypy are required for the complete development gate and are pinned separately from the application:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'ruff==0.14.14' 'mypy==2.1.0'
```

The same pins are recorded in the `dev` dependency group in `pyproject.toml`.

## Validation

Run focused tests while working. Test modules use `unittest`, for example:

```sh
PYTHONPATH=src python -m unittest -v tests.test_http
```

Before opening a pull request, run the complete local release gate:

```sh
make release-check
```

The gate compiles the source, runs all tests, runs Ruff and mypy, builds `dist/llmits`, checks the embedded MIT license and normalized archive metadata, rebuilds for a byte-for-byte comparison, and performs credential-free artifact smoke tests. It does not need provider credentials and must not contact live provider APIs.

Individual commands are also available:

```sh
make check
make lint
make typecheck
make build
make history-check
```

`make history-check` scans every commit message, historical path, and unique blob reachable from `HEAD` — including content deleted before the current tip — for credentials, private session URLs, and other prohibited content. It prints a machine-readable report and exits nonzero when it finds anything. It is part of `make release-check`.

## Commit identity and local gates

This repository's history is public. Configure a GitHub no-reply identity before committing so a personal email address never enters the history:

```sh
git config user.name "Your Name"
git config user.email "1234567+username@users.noreply.github.com"
```

GitHub shows your exact no-reply address under *Settings → Emails*. The commands above configure one clone; add `--global` to set it everywhere.

Install the tracked pre-push gate once per clone:

```sh
make install-hooks
```

This sets `core.hooksPath` to `.githooks/`, so `git push` first runs the full-history scan and then the complete release gate. Hooks are bypassable with `git push --no-verify`, so they are defense in depth for the person pushing; continuous integration is the authoritative boundary.

## Change guidelines

- Keep Python 3.11–3.14 compatibility and do not add runtime dependencies.
- Keep credential reads fail-closed and read-only. Never add credentials or provider responses to the repository, logs, exceptions, or snapshots.
- Keep provider hosts and request paths fixed in source. Tests must use fake transports and invented response data, not live services.
- Put synthetic response fixtures in `tests/fixtures/`. Use obviously invented plans, identifiers, quota values, and dates; do not adapt payloads captured from an account.
- Add a regression test for behavior changes. Security boundary changes should include a test that fails when the unsafe behavior is restored.
- Update public documentation when flags, output, supported versions, security behavior, or packaging contracts change.
- Do not include `dist/`, bytecode, virtual environments, tool caches, credentials, or machine-local agent state in a pull request.

## Task tracking

Bugs, regressions, and feature requests are tracked publicly in [GitHub Issues](https://github.com/mcrescenzo/llmits/issues). Link the issue a pull request addresses, or open one before starting nontrivial work, so every change stays tied to a described problem.

## Pull requests

A pull request should explain the user-visible or security-relevant effect, list the exact validation commands run, and call out any check that could not be run. Keep unrelated formatting or refactoring out of the same change. CI must pass on every supported Python version before merge.

## Preparing a public-history candidate

Maintainers can create a clean, one-commit publication input from the current converged tree without changing this repository's refs or remotes:

```sh
make public-history OUTPUT=/tmp/llmits-public-history
```

`OUTPUT` must name a new directory outside the source repository. The command includes tracked and non-ignored working-tree files, omits ignored local state, uses the repository owner's GitHub no-reply identity, scans the candidate commit and blobs, and prints a machine-readable verification report. Two runs from the same tree must print the same candidate commit ID. The source repository's refs, remotes, and index are left unchanged, even when the calling environment exports repository-routing variables such as `GIT_DIR` or injects Git configuration through `GIT_CONFIG_COUNT`/`GIT_CONFIG_PARAMETERS`. The candidate's file set follows the repository's own ignore rules (`.gitignore` files and `.git/info/exclude`) only: a machine-local global excludes file is overridden explicitly, including Git's built-in default at `$XDG_CONFIG_HOME/git/ignore`, so candidate contents do not depend on the machine's Git configuration. The command does not push, publish, tag, change visibility, or authorize release.

## Reporting security issues

Do not disclose vulnerabilities, exploits, credentials, account data, or provider responses in a public pull request or issue. Follow the private advisory process in [SECURITY.md](SECURITY.md). Public issues are appropriate only for non-sensitive bugs or for a sanitized request to establish private contact.

If a credential, token, private session URL, or account-derived payload was committed — even in a commit later deleted from the tip:

1. Revoke or rotate the credential with its provider first; repository history is secondary.
2. Report privately through the advisory process in [SECURITY.md](SECURITY.md) without reproducing the leaked value.
3. Coordinate any rewrite of published history with a maintainer before pushing it.
4. Afterward, `make history-check` and `make release-check` must pass from a fresh clone before further work continues.
