"""Neutral temporal context plugin for Hermes.

The plugin injects the current local time into the current user turn at
API-call time via the ``pre_llm_call`` hook. It is intentionally config-driven:
no user name, tenant name, locale, or timezone is hardcoded in this module.

Example config.yaml::

    plugins:
      enabled: [temporal_context]
    temporal_context:
      enabled: true
      timezone: America/New_York
      display_name: "Operator"
      relative_time_warning: true
      output_guard:
        enabled: false
        replacements: []
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_TIMEZONE = "UTC"
_DEFAULT_WARNING = (
    "Relative time/daypart claims require an explicit timestamp from tools, "
    "messages, or provided context."
)


def _load_plugin_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
        raw = cfg_get(cfg, "temporal_context", default={})
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


def _settings(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg: Mapping[str, Any] = config if config is not None else _load_plugin_config()
    timezone_name = str(cfg.get("timezone") or _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE
    display_name = str(cfg.get("display_name") or "").strip()
    warning_enabled = _truthy(cfg.get("relative_time_warning"), default=True)
    enabled = _truthy(cfg.get("enabled"), default=True)
    return {
        "enabled": enabled,
        "timezone": timezone_name,
        "display_name": display_name,
        "relative_time_warning": warning_enabled,
        "warning": str(cfg.get("warning") or _DEFAULT_WARNING).strip() or _DEFAULT_WARNING,
        "output_guard": cfg.get("output_guard") if isinstance(cfg.get("output_guard"), dict) else {},
    }


def _zoneinfo(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(_DEFAULT_TIMEZONE)


def build_temporal_context(
    *,
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> str | None:
    """Build an ephemeral temporal-context block, or ``None`` when disabled.

    ``now`` is injectable for tests. Naive datetimes are treated as UTC.
    """

    settings = _settings(config)
    if not settings["enabled"]:
        return None

    tz_name = settings["timezone"]
    tz = _zoneinfo(tz_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(tz)
    rendered = local.strftime("%Y-%m-%d %H:%M %Z (%z)")

    label = f" for {settings['display_name']}" if settings["display_name"] else ""
    parts = [
        f"[Temporal context: current local time{label} is {rendered}.",
        f"Time zone: {tz_name}.",
    ]
    if settings["relative_time_warning"]:
        parts.append(settings["warning"])
    return " ".join(parts) + "]"


def pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    """Hermes ``pre_llm_call`` hook: append context to the current user message."""

    context = build_temporal_context(
        now=kwargs.get("now"),
        config=kwargs.get("config"),
    )
    if not context:
        return None
    return {"context": context}


def transform_llm_output(text: str | None = None, **kwargs: Any) -> str:
    """Optional conservative output transform configured entirely by the user.

    Defaults to no-op. Hermes calls this hook with ``response_text=...``;
    tests may pass ``text`` positionally. If enabled, applies literal/regex
    replacements supplied under ``temporal_context.output_guard.replacements``.
    This keeps policy and language choices out of the public plugin code.
    """

    source_text = text if text is not None else str(kwargs.get("response_text") or "")
    cfg = kwargs.get("config") if isinstance(kwargs.get("config"), Mapping) else _load_plugin_config()
    settings = _settings(cfg)
    guard = settings.get("output_guard") or {}
    if not _truthy(guard.get("enabled"), default=False):
        return source_text
    replacements = guard.get("replacements")
    if not isinstance(replacements, list):
        return source_text

    result = source_text
    for item in replacements:
        if not isinstance(item, Mapping):
            continue
        pattern = item.get("pattern")
        replacement = item.get("replacement")
        if not isinstance(pattern, str) or not isinstance(replacement, str):
            continue
        if _truthy(item.get("regex"), default=False):
            try:
                result = re.sub(pattern, replacement, result)
            except re.error:
                continue
        else:
            result = result.replace(pattern, replacement)
    return result


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("transform_llm_output", transform_llm_output)
