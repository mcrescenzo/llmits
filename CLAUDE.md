# Repository guidance for coding agents

This file is optional guidance for automated contributors. Human contributors should start with [CONTRIBUTING.md](CONTRIBUTING.md). The repository does not require a particular agent runtime, local issue tracker, home-directory skill, or ignored file.

## Project contract

- Support Python 3.11 through 3.14 and keep application runtime dependencies empty.
- Keep credential discovery read-only. Never refresh, rewrite, cache, log, or print credentials.
- Keep network destinations and paths compile-time fixed; do not add proxy, redirect, or caller-controlled egress.
- Treat provider payloads and exception messages as untrusted. Normalize and sanitize them before JSON or TUI output.
- Use invented provider data in tests. Do not record live provider responses or make live provider requests.
- Preserve deterministic, license-bearing zipapp output.

## Working practices

1. Read the relevant source and tests before changing behavior.
2. Add or update a focused regression test for behavioral changes.
3. Run targeted tests during development, then `make release-check` before declaring the change complete.
4. Do not commit secrets, local paths, credentials, generated artifacts, tool caches, or machine-local state.
5. Keep pull requests focused and update README, SECURITY, or CONTRIBUTING when their contracts change.

## Commands

After installing the pinned development tools described in CONTRIBUTING:

```sh
make check          # compile source and run the full standard-library test suite
make lint           # Ruff over src/llmits
make typecheck      # mypy over src/llmits
make build          # deterministic zipapp at dist/llmits
make release-check  # complete local release gate (includes the history scan)
make history-check  # scan commit messages, historical paths, and reachable blobs in full HEAD ancestry
make install-hooks  # point core.hooksPath at the tracked .githooks/ pre-push gate
make public-history OUTPUT=/tmp/llmits-public-history  # isolated clean-root candidate
```

## Public-history hygiene

The repository history is public. Commit with a GitHub no-reply identity (see
CONTRIBUTING.md), never commit credentials or machine-local state, and run
`make install-hooks` once per clone so pushes run the same history gate and
release check as CI. Track public work in GitHub Issues.

Security-sensitive findings belong in the private reporting channel documented in [SECURITY.md](SECURITY.md), not in public issues or test fixtures.
