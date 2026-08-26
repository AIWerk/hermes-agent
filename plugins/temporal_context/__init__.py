"""Deterministic per-turn local temporal context for Hermes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_TIMEZONE = "UTC"
_DEFAULT_WARNING = (
    "Relative time/daypart claims require an explicit timestamp from tools, "
    "messages, or provided context."
)
_CONTEXT_MARKER = "[Temporal context:"
_CONTEXT_KEY = "temporal_current_origin"


def _load_plugin_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import cfg_get, load_config

        raw = cfg_get(load_config(), "temporal_context", default={})
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return default


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _settings(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg: Mapping[str, Any]
    if config is None:
        cfg = _load_plugin_config()
    elif isinstance(config, Mapping):
        cfg = config
    else:
        cfg = {}
    return {
        "enabled": _truthy(cfg.get("enabled"), default=True),
        "timezone": _text(cfg.get("timezone"), _DEFAULT_TIMEZONE),
        "display_name": _text(cfg.get("display_name")),
        "relative_time_warning": _truthy(
            cfg.get("relative_time_warning"), default=True
        ),
        "warning": _text(cfg.get("warning"), _DEFAULT_WARNING),
    }


def _zoneinfo(timezone_name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def _daypart(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def build_temporal_context(
    *,
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Build a local-time context block from an aware runtime timestamp."""

    settings = _settings(config)
    if not settings["enabled"]:
        return None

    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        return None

    timezone_name = settings["timezone"]
    zone = _zoneinfo(timezone_name)
    if zone is None:
        return None
    local = current.astimezone(zone)
    rendered = local.strftime("%Y-%m-%d %H:%M %Z (%z)")

    label = f" for {settings['display_name']}" if settings["display_name"] else ""
    parts = [
        f"{_CONTEXT_MARKER} current local time{label} is {rendered}.",
        f"Time zone: {timezone_name}.",
        f"Daypart: {_daypart(local.hour)}.",
    ]
    if settings["relative_time_warning"]:
        parts.append(settings["warning"])
    return " ".join(parts) + "]"


def pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    """Inject temporal context into the current API-bound user turn."""

    context = build_temporal_context(
        now=kwargs.get("now"),
        config=kwargs.get("config"),
    )
    return {"context": context, "context_key": _CONTEXT_KEY} if context else None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)
