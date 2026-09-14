# llmits

A Linux terminal dashboard that shows your LLM provider subscription limits in one pane:

- **Claude** (Pro/Max): 5-hour window, 7-day window, model-scoped weekly limits, extra usage.
- **Codex / ChatGPT**: primary and weekly rate-limit windows, per-model sub-limits, credits, banked limit resets.
- **Z.AI GLM Coding Plan**: 5-hour and weekly quotas plus the monthly MCP allowance.
- **Kimi Coding Plan**: 5-hour rolling window and weekly membership quota.

`llmits` is deliberately small and conservative:

- Python 3.11+ standard library only at runtime; tests also use the standard library.
- Read-only credential access; it never writes, refreshes, or caches credentials.
- Talks only to four fixed hosts over verified HTTPS; no proxies, no redirects.
- No telemetry, no updater, no subprocesses, no clipboard, no config files.

## Install

Build the single-file executable:

```sh
make build          # produces dist/llmits
cp dist/llmits ~/.local/bin/llmits
```

Run from a checkout during development:

```sh
PYTHONPATH=src python3 -m llmits
```

## Usage

```sh
llmits              # interactive TUI
llmits --json       # one-shot JSON snapshot for scripts
llmits --diagnose   # check credentials, fixed endpoints, and payload compatibility
```

| Flag | Meaning |
| --- | --- |
| `--json` | Print one JSON snapshot and exit; never starts the TUI. |
| `--diagnose` | Check credential discovery, the fixed provider endpoint, and payload compatibility for each selected provider, then exit. Providers without a discovered credential are not contacted. Output contains only app/Python/platform versions and fixed local classifications—never credential values, paths, source names, payloads, or exception text. |
| `--fail-used-percent N` | With `--json` only: exit `3` when every requested provider succeeded and any reported window is used at least `N` percent (0–100, inclusive). A provider failure keeps priority and exits `1`; the JSON document itself is unchanged. |
| `--providers LIST` | Comma-separated subset of `claude,codex,zai,kimi` (default: all four). |
| `--refresh-seconds N` | TUI auto-refresh interval. Default 300; `0` disables; values 1–59 are rejected. |
| `--claude-credentials PATH` | Explicit Claude credentials file. |
| `--codex-credentials PATH` | Explicit Codex `auth.json`. |
| `--version` / `--help` | Version and usage. |

TUI: the footer is pinned to the last row — `r/R refresh  ·  j/k scroll  ·  q/esc quit  ·  tab focus  ·  h hide  ·  c collapse  ·  a show all` — and the arrow keys scroll like `j`/`k`. `tab` (and shift-tab) moves the `> ` focus marker between cards and scrolls it into view; `R` refreshes only the focused provider while `r` refreshes every visible one; `h` hides the focused card (hidden cards are excluded from `r` refreshes), `c` collapses it to a header-only card, and `a` restores every hidden and collapsed card and refreshes them all. The state is session-only and never persisted. The terminal must be at least 60×15, the size at which the card stack is scrollable so every requested provider stays reachable. Compact mode depends on width alone: 80 columns or fewer drops the local reset clock and per-window token counts from the bar rows, regardless of height. Bar glyphs are Unicode (`━ ─ ● ○ · …`) when the terminal locale's codeset is UTF-8, else plain ASCII (`= - * o - ...`); `locale.setlocale(LC_ALL, "")` runs first, and the choice follows that C-library locale rather than Python's own stdout encoding, so a forced `LC_ALL=C` gets ASCII instead of a corrupted screen.

Sample render at 80 columns, from the test fixtures (no live data or credentials involved):

```
llmits v0.2.0  ·  updated 12s ago  ·  next refresh 4:48

> Claude  [Claude Pro/Max]  ● 12s ago
  5h      ━━━━━━━━━━━━━━━━━───────────────────────   42%  resets now
  7d      ━━━━────────────────────────────────────   10%  resets 6d 7h
  Opus    ━━──────────────────────────────────────    5%  resets 6d 7h
  extra   ━━━━━━━━━━━━━━━━━───────────────────────   42%

Codex  [Codex plus]  ● 12s ago
  5h      ━━━━━━━━━───────────────────────────────   23%  resets 5d 20h
  credits $42.50 balance
  banked  2 available

Z.AI  [Z.AI pro]  ● stale · 6m ago
  5h      ━━━─────────────────────────────────────    7%  resets now
  last update failed: provider server error (HTTP 503)

r/R refresh  ·  j/k scroll  ·  q/esc quit  ·  tab focus  ·  h hide  ·  c collapse  ·  a show all
```

Exit codes: `0` success, `1` a requested provider failed (this outranks the threshold result), `2` invalid invocation, non-TTY interactive run, or a fatal internal error, `3` every requested provider succeeded but a window met the `--fail-used-percent` threshold, `130` if the TUI is interrupted (Ctrl-C).

## Credentials

`llmits` reads existing credentials and never modifies them.

- **Claude** — first match of: `--claude-credentials`, `LLMITS_CLAUDE_CREDENTIALS`, `$CLAUDE_CONFIG_DIR/.credentials.json`, `~/.claude/.credentials.json`. Log in with the Claude Code CLI (`claude login`) when the token expires.
- **Codex** — first match of: `--codex-credentials`, `LLMITS_CODEX_CREDENTIALS`, `~/.codex/auth.json`. Log in with the Codex CLI when the token expires.
- **Z.AI** — first usable source in this order:
  1. `ZAI_API_KEY`, then `ZHIPU_API_KEY`.
  2. `~/.pi/agent/auth.json`, entry `zai`, when it contains a literal `api_key` credential.
  3. `$CLAUDE_CONFIG_DIR/settings.json` or `~/.claude/settings.json`, but only when the adjacent `ANTHROPIC_BASE_URL` matches `https://api.z.ai/api/anthropic`, with or without one trailing slash — a byte-for-byte, case-sensitive comparison; nothing else about the value is normalized.
  4. `$CODEX_HOME/config.toml` or `~/.codex/config.toml`, but only when `[model_providers.ZAI].base_url` matches `https://api.z.ai/api/v1`, with or without one trailing slash — the same byte-for-byte comparison.
- **Kimi Coding Plan** — first usable source in this order:
  1. `~/.pi/agent/auth.json`, entry `kimi-coding`, when it contains a literal `api_key` credential.
  2. `$KIMI_CODE_HOME/config.toml` or `~/.kimi-code/config.toml`, but only when a `[providers.<name>]` entry's `base_url` matches `https://api.kimi.com/coding/v1`, with or without one trailing slash — the same byte-for-byte binding rule as Z.AI. Entries pointed at `api.moonshot.ai` or any other base are skipped, as are entries with no `base_url` at all. The generic `KIMI_API_KEY` environment variable is deliberately ignored because it can also contain a pay-as-you-go Moonshot key and therefore is not provider-bound.

These Z.AI locations and schemas are documented by [Z.AI's Claude Code guide](https://docs.z.ai/devpack/tool/claude), [Z.AI's Codex guide](https://docs.z.ai/devpack/tool/codex), and [Pi's provider documentation](https://github.com/earendil-works/pi-mono/blob/main/packages/coding-agent/docs/providers.md). The Kimi locations follow the same pattern and are documented by [Kimi's data-locations guide](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/data-locations.html) and Pi's provider documentation. A generic Anthropic-compatible token is never assumed to be a Z.AI key: its adjacent base URL must bind it to Z.AI first, and a `~/.kimi-code/config.toml` key is only used when its entry's base URL binds it to the Kimi Coding Plan. Pi shell-command and environment-expression credentials are deliberately ignored; `llmits` never executes credential helpers. Kimi's OAuth credential files (`~/.kimi-code/credentials/`) are not read: kimi-cli owns their refresh.

Credential files must be regular files, owned by you, not readable by group or other (`chmod 600`), and at most 1 MiB. A strict Claude or Codex credential file that fails these checks reports an authentication error. Optional cross-tool discovery files that are unsafe, malformed, or not provider-bound are skipped, allowing the next source in the precedence chain to be tried; unsafe file contents are never read.

## Extending credential discovery

Credential lookup is provider-neutral. `CredentialSpec` defines an ordered chain, while `EnvironmentSource` and `StructuredFileSource` are reusable source adapters behind the `CredentialSource` interface. Structured sources receive a secure JSON or TOML loader plus a provider-specific extractor that must validate provider identity or an exact base URL before returning a secret.

When adding a provider or source:

1. Use an exact documented path; never scan directories or shell startup files.
2. Bind every source to its provider audience and validate ambiguous tool configurations before returning a key.
3. Reuse the secure loaders so ownership, permissions, file type, symlink, and size checks remain enforced.
4. Register the provider reader and keep its id aligned with the provider fetcher registry.
5. Add positive precedence tests, wrong-audience/endpoint tests, and sentinel-token leakage tests.

## JSON output

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-14T02:31:07Z",
  "providers": [
    {
      "provider": "claude",
      "status": "available",
      "plan_name": "Claude Pro/Max",
      "fetched_at": "2026-07-14T02:31:06Z",
      "stale": false,
      "windows": [
        {
          "key": "5h",
          "label": "5h",
          "used_percent": 42,
          "remaining_percent": 58,
          "reset_at": "2026-07-13T10:00:00Z",
          "period_seconds": 18000,
          "used_value": null,
          "limit_value": null,
          "remaining_value": null
        }
      ],
      "error": null
    }
  ]
}
```

The output never contains raw provider responses, tokens, account identifiers, or credential paths. Statuses: `available`, `auth_required`, `unavailable`, `rate_limited`, `network_error`, `parse_error`.

## Security

See [SECURITY.md](SECURITY.md) for the threat model, egress allowlist, and hardening contract. The installed zipapp does not create application caches, logs, config, credential files, or TLS key logs. Running from a source checkout may create normal Python `__pycache__` bytecode directories, and `make build` intentionally writes `dist/llmits`. Network access—including `--diagnose` endpoint checks—is limited to four fixed HTTPS hosts; error text and provider-derived identifiers are replaced with bounded local values before JSON or TUI rendering.

All tracked provider-response fixtures are synthetic. They contain invented plans, quotas, timestamps, and identifiers and are used only for offline parser and rendering tests.

## Caveats

The usage endpoints for Claude subscriptions, Codex/ChatGPT subscriptions, Z.AI plans, and Kimi Coding Plans are undocumented or only partially documented and can change without notice. When a provider changes its response shape, that provider's card reports a parse error instead of guessing; update this tool when that happens.

Deliberately out of scope, with the deciding evidence:

- **GitHub Copilot** is not implemented. Its only remaining-quota source is the undocumented internal `api.github.com/copilot_internal/user` endpoint whose quota semantics changed with GitHub's June 2026 billing migration; GitHub's documented per-user billing endpoints embed a dynamic `{username}` path segment (violating llmits' fixed compile-time path rule) and report spend, not remaining quota. Revisit only if GitHub ships a documented per-user quota endpoint.
- **Kimi** reports only the evidenced windows (5-hour and weekly). The payload's `totalQuota` monthly membership-freeze flag, `parallel` concurrency cap, and Extra Usage wallet balance are dropped rather than guessed: their exact shapes are unverified, and a wrong guess would show wrong numbers instead of failing closed. `~/.kimi-code/credentials/` OAuth tokens are not read (kimi-cli owns their refresh).
- **OpenCode (Zen/Go)** is not implemented. Its first-party usage endpoint (`GET https://opencode.ai/zen/go/v1/usage`) is well-evidenced, but its percent-only windows do not establish whether `percent` reports the used or the remaining share of a window, and no first-party evidence available offline settles the direction. Guessing would risk silently inverted quota bars, so the provider is deferred until the endpoint's response semantics are documented or its source confirms the direction. (An earlier draft of this release also identified that its multi-provider `auth.json` needs strictly entry-bound key extraction before shipping.)

## Testing

```sh
make test    # verbose test suite
make check   # compile check + full test suite
make lint    # ruff over src/llmits (config in pyproject.toml)
make typecheck  # mypy over src/llmits (config in pyproject.toml)
```

`make test` and `make check` are the canonical commands. `tools/verify.sh`
runs the same suite and adds a fallback for sandboxed agent runtimes that
block importing project packages from test files. The lint and type-check tools are pinned development-only dependencies in the `dev` dependency group in `pyproject.toml`; neither is installed or imported by the shipped application. `make release-check` runs the full local release gate: tests, Ruff, mypy, deterministic build checks, embedded-license inspection, and artifact smoke tests.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, validation, fixture, and pull-request guidance. Report sensitive findings through the private process in [SECURITY.md](SECURITY.md), not in a public issue or pull request.

## License

`llmits` is released under the [MIT License](LICENSE). The deterministic zipapp includes the complete notice at `llmits/LICENSE`.
