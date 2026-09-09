"""Upstream adapter registry for the local proxy server (see :class:`UpstreamAdapter`)."""

from typing import Dict, Type

from hermes_cli.proxy.adapters.base import UpstreamAdapter
from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter
from hermes_cli.proxy.adapters.openai_codex import OpenAICodexAdapter
from hermes_cli.proxy.adapters.xai import XAIGrokAdapter

# Registry of available adapter classes keyed by provider name as used on
# the ``hermes proxy start --provider <name>`` CLI flag.
ADAPTERS: Dict[str, Type[UpstreamAdapter]] = {
    "nous": NousPortalAdapter,
    "openai-codex": OpenAICodexAdapter,
    "xai": XAIGrokAdapter,
}


def get_adapter(name: str) -> UpstreamAdapter:
    """Instantiate an adapter by provider name."""
    key = (name or "").strip().lower()
    if key not in ADAPTERS:
        available = ", ".join(sorted(ADAPTERS)) or "(none)"
        raise ValueError(f"Unknown proxy upstream provider: {name!r}. Available: {available}")
    return ADAPTERS[key]()


__all__ = ["UpstreamAdapter", "ADAPTERS", "get_adapter"]
