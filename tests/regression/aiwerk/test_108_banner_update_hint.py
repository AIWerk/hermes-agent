"""Regression coverage for localized banner update hints."""

from __future__ import annotations


def test_update_hint_uses_hungarian_locale(monkeypatch) -> None:
    from agent.i18n import reset_language_cache
    from hermes_cli import banner

    monkeypatch.setenv("HERMES_LANGUAGE", "hu")
    monkeypatch.setattr(
        "hermes_cli.config.recommended_update_command", lambda: "hermes update"
    )
    reset_language_cache()
    try:
        rendered = banner._format_update_notice(2)
    finally:
        reset_language_cache()

    assert "2 committal lemaradva" in rendered
    assert "frissítés:" in rendered
    assert "2 commits behind" not in rendered


def test_update_available_hint_uses_hungarian_locale(monkeypatch) -> None:
    from agent.i18n import reset_language_cache
    from hermes_cli import banner

    monkeypatch.setenv("HERMES_LANGUAGE", "hu")
    monkeypatch.setattr(
        "hermes_cli.config.get_managed_update_command", lambda: "hermes update"
    )
    reset_language_cache()
    try:
        rendered = banner._format_update_notice(-1)
    finally:
        reset_language_cache()

    assert "frissítés elérhető" in rendered
    assert "futtasd:" in rendered
    assert "update available" not in rendered
