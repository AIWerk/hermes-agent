"""Tool wrapper for AI Notes static HTML publishing."""

from __future__ import annotations

from datetime import date
from typing import Any

from hermes_cli.ai_notes import publish_html_note
from hermes_cli.config import load_config
from tools.registry import registry, tool_error, tool_result


def check_ai_notes_requirements() -> bool:
    try:
        cfg = load_config().get("ai_notes", {})
        if not cfg.get("enabled", False):
            return False
        if not cfg.get("public_base_url") or not cfg.get("publish_root"):
            return False
        visibility = str(cfg.get("visibility") or "public_static_html")
        from hermes_cli.ai_notes import _validate_public_base_url

        _validate_public_base_url(
            str(cfg.get("public_base_url") or ""),
            allow_local=visibility.startswith("local_only"),
        )
        return True
    except Exception:
        return False


def publish_ai_note_tool(
    *,
    html: str,
    title: str,
    slug: str | None = None,
    today: date | None = None,
) -> str:
    """Publish a safe static HTML note using the ai_notes config section."""
    try:
        cfg = load_config().get("ai_notes", {})
        result = publish_html_note(
            html=html,
            title=title,
            slug=slug,
            config=cfg,
            today=today,
        )
        return tool_result(result)
    except Exception as exc:
        return tool_error(str(exc), ok=False)


PUBLISH_AI_NOTE_SCHEMA: dict[str, Any] = {
    "name": "publish_ai_note",
    "description": (
        "Publish customer-readable static HTML to the configured AI Notes "
        "domain/root and return the public URL. Use for generated HTML pages, "
        "visual reports, mini-sites, or static artifacts that should be opened "
        "from the CUI as a link. The tool refuses disabled configs, local paths, "
        "localhost/private URLs, MEDIA tags, and secret-like content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "html": {
                "type": "string",
                "description": "Complete static HTML document or fragment to publish.",
            },
            "title": {
                "type": "string",
                "description": "Human-readable title for the note.",
            },
            "slug": {
                "type": "string",
                "description": "Optional URL slug. It is sanitized before writing.",
            },
        },
        "required": ["html", "title"],
    },
}


registry.register(
    name="publish_ai_note",
    toolset="ai_notes",
    schema=PUBLISH_AI_NOTE_SCHEMA,
    handler=lambda args, **kw: publish_ai_note_tool(
        html=args.get("html", ""),
        title=args.get("title", ""),
        slug=args.get("slug"),
    ),
    check_fn=check_ai_notes_requirements,
    emoji="📝",
)
