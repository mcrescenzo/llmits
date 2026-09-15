"""Shared test support for hermetic credential environments.

The variables below live in one place because several test modules must clear
exactly the same set before exercising credential discovery. A developer shell
that exports a real credential must never satisfy, break, or leak into a test,
and per-module copies of this list are how that guarantee drifts: add every
variable a new credential source consults here, once.
"""
from __future__ import annotations

# Credential-locating variables honored by this application or by the provider
# tools whose files it reads.
CREDENTIAL_ENV_VARS = (
    "LLMITS_CLAUDE_CREDENTIALS",
    "LLMITS_CODEX_CREDENTIALS",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "ZAI_API_KEY",
    "ZHIPU_API_KEY",
    "KIMI_API_KEY",
    "KIMI_CODE_HOME",
    "XDG_DATA_HOME",
    "OPENCODE_AUTH_CONTENT",
)
