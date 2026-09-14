"""Provider registry: fixed identifiers, display names, and fetch entry points."""
from __future__ import annotations

from . import claude, codex, kimi, zai

PROVIDER_IDS = ("claude", "codex", "zai", "kimi")
DISPLAY_NAMES = {
    "claude": "Claude",
    "codex": "Codex",
    "zai": "Z.AI",
    "kimi": "Kimi",
}
FETCHERS = {
    "claude": claude.fetch,
    "codex": codex.fetch,
    "zai": zai.fetch,
    "kimi": kimi.fetch,
}


def display_name(provider_id: str) -> str:
    return DISPLAY_NAMES.get(provider_id, provider_id)


def is_known(provider_id: str) -> bool:
    return provider_id in FETCHERS
