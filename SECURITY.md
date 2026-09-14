# Security Policy

`llmits` reads subscription credentials. This document is the contract that code review and tests enforce.

## Supported versions

Security fixes are made against the latest released `llmits` version on supported Python 3.11, 3.12, 3.13, and 3.14. Reports against older `llmits` releases are still useful, but maintainers may ask reporters to confirm the issue on the latest release.

## Threat model

- The tool runs on a single-user Linux machine and reads files owned by that user.
- Assets at risk: Claude/Codex OAuth tokens, the Z.AI API key, and the contents of provider usage responses (which may include account metadata).
- Adversaries considered: other local users reading files or environment leakage, hostile or poisoned environments (injected env vars, symlinks, hostile `PATH`), and malicious/compromised provider endpoints or intermediaries.

## Non-goals

- No credential issuance, refresh, rotation, command-based credential helper execution, or environment-expression evaluation. Official tools own login.
- No application persistence during normal zipapp use: no cache, config, history, credential, or log files are created. Python may create `__pycache__` directories when the package is run from a source checkout, and maintainer commands intentionally write build artifacts such as `dist/llmits`; neither contains credentials or provider responses.
- No broad credential search: no directory scans, shell-startup parsing, keychain access, or undocumented store formats.
- No outbound surface beyond the three fixed usage endpoints.

## Credential handling

- Credentials come only from exact documented environment variables and file paths in the precedence chains listed in README. Cross-tool stores are supported only when their provider identity or adjacent base URL can be validated before the key is returned.
- `CredentialSpec` supplies an ordered provider-bound chain through the `CredentialSource` interface. The built-in `EnvironmentSource` and `StructuredFileSource` adapters can be reused by future providers without weakening the file or audience checks.
- Every source declares a provider audience; discovery rejects a source whose audience differs from its specification. The service registry uses the same provider ids as the fixed-host fetcher registry.
- Files are opened with `os.open(path, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)`; symlinks are rejected by the kernel and FIFOs cannot block discovery.
- The file must be a regular file, owned by the effective UID, with no group or other permission bits, and at most 1 MiB. Oversized or permissive files are refused with guidance to `chmod 600`; the tool never changes permissions itself.
- Only the specific token field is extracted; the file descriptor is closed immediately. JSON and TOML are parsed with Python's standard library.
- Optional cross-tool sources are skipped when absent, insecure, malformed, or not bound to the expected provider. Strict provider-owned credential files remain fail-closed.
- Z.AI keys in Claude or Codex configuration are accepted only beside the exact documented global Z.AI base URL. China-plan and arbitrary proxy URLs are not retargeted to `api.z.ai`.
- Pi `auth.json` discovery accepts only the `zai` provider's literal `api_key`. Shell-command and environment-expression values are ignored rather than executed or interpreted.
- Errors never contain tokens, authorization headers, provider-supplied configuration values, or credential paths.

## Network egress (fixed allowlist)

| Provider | Host | Path |
| --- | --- | --- |
| Claude | `api.anthropic.com` | `GET /api/oauth/usage` |
| Codex | `chatgpt.com` | `GET /backend-api/wham/usage` |
| Z.AI | `api.z.ai` | `GET /api/monitor/usage/quota/limit` |

- Hosts and paths are compile-time constants inside provider adapters. CLI options and environment variables cannot influence a destination.
- TLS contexts are built by an explicit factory (`create_tls_context` in `src/llmits/http.py`), not `ssl.create_default_context()`: hostname checks and certificate verification are always required, and the trust store comes only from the interpreter's compiled-in CA locations. Ambient `SSLKEYLOGFILE` is never honored (no key-log file is created or written), and ambient `SSL_CERT_FILE`/`SSL_CERT_DIR` cannot redirect the trust store; if the compiled-in locations are missing, verification fails closed rather than falling back to environment-controlled paths. No proxy environment variables are honored; redirects are never followed (a redirect is reported as a provider error).
- Requests time out after 10 seconds; response bodies are capped at 1 MiB.
- `User-Agent` is exactly `llmits/<version>`.
- Z.AI retries once with a `Bearer` header against the same fixed host/path only after a 401/403, mirroring the official Z.AI plugin's authentication variant.

## Output hygiene

- Provider adapters normalize responses into typed snapshots; raw response bodies never cross the adapter seam.
- JSON output is structurally limited to normalized fields. Error text is sanitized (control characters stripped, length capped) and never includes response bodies or tokens. Provider error text is fixed local wording; provider `msg` fields are never echoed. Transport exception text is never surfaced either: network failures keep a stable `network_error` classification with a fixed local message and only the exception class name.
- Upstream limit identifiers are treated as untrusted at the adapter boundary: recognized ones map to a fixed local vocabulary (e.g. `spk` -> "Spark"), and anything unrecognized gets a collision-safe ordinal id (`x1` -> "Limit 1") that contains no provider-derived characters, so raw provider strings never reach keys, labels, JSON, or the TUI.
- Provider-derived plan/level strings are vetted against a strict shape before use and sanitized and length-capped again at the model boundary, so terminal escape sequences (including C1/OSC) cannot reach JSON or the TUI.
- The TUI renders only normalized snapshots.

## Guarantees enforced by tests

- `tests/test_security.py` statically rejects forbidden imports (`subprocess`, `urllib`, `webbrowser`, `ctypes`, `shutil`, …) and forbidden calls (`eval`, `exec`, `os.system`, …) in `src/`.
- The same test rejects application file-writing APIs in `src/` (write-mode `open`, `os.remove`, `Path.write_text`, …) and restricts `os.open` flags to the read-only credential open.
- Sentinel tests assert that credentials, provider-derived limit names, and transport exception text never appear in snapshots, JSON, or TUI output.
- TLS tests prove ambient key-log and trust-store variables are ignored while certificate and hostname verification remain required.
- Packaging tests build twice, compare bytes, inspect normalized archive metadata, verify the complete MIT notice, and exercise the artifact without credentials.
- Provider-response fixtures under `tests/fixtures/` are synthetic; their plan names, quota values, identifiers, and reset times are invented and tests do not contact provider services.
- The application and its tests use only the Python standard library at runtime. Ruff and mypy are pinned development-only tools and are not runtime dependencies.

## Reporting a vulnerability

Please report sensitive security issues through a [private GitHub security advisory](https://github.com/mcrescenzo/llmits/security/advisories/new). Include a description, affected version, reproduction steps, and impact, but do not include live credentials, tokens, or provider-response data unless a maintainer explicitly arranges a safe transfer.

If private advisory reporting is unavailable, open a [public issue](https://github.com/mcrescenzo/llmits/issues) containing only non-sensitive contact and summary information. Do not post exploit details, secrets, private URLs, account data, or provider responses in the public tracker.

Provider endpoint changes that produce a parse error but have no security impact may be reported as ordinary public bugs with sanitized fixtures.

## Maintainer maintenance and incident checklist

These hosting safeguards and release checks recur: verify them before every release, after any security incident, and at least quarterly. This document does not claim they are currently enabled.

- [ ] Confirm private vulnerability reporting is enabled and the advisory form is reachable.
- [ ] Confirm GitHub secret scanning and push protection are enabled for the repository.
- [ ] Protect the release branch and require the pinned Python 3.11–3.14 CI matrix, which checks out full history (`fetch-depth: 0`) and runs the history scan through `make release-check`.
- [ ] Run `make release-check` — which includes `make history-check`, the full-ancestry scan of commit messages, historical paths, and every unique reachable blob — from a clean checkout and record the resulting artifact SHA-256.
- [ ] Build the artifact a second time, confirm the bytes and checksum match, and inspect `llmits/LICENSE` in the archive.
- [ ] Run `make public-history OUTPUT=/path/outside/repository` twice and confirm both candidate commit IDs match; retain its clean-history scan report for review.

Incident response for exposed credentials or account data in repository history:

- Revoke and rotate the credential with its provider before touching repository history.
- Use the private advisory channel; never reproduce the leaked value in issues, pull requests, or test fixtures.
- Coordinate any rewrite of published history with all maintainers and announce it; prefer revocation over history erasure when both are possible.
- After remediation, rerun the recurring checks above — including `make history-check` against a fresh full clone — and re-verify push protection.
