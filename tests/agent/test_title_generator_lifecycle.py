"""Behavioral contract for the session-title lifecycle.

Ported from the AIWerk three-step lifecycle test at origin blob
2c9eaa95817620a88f1586aefa439cfe8460e04c and its title-history commits.
The assertions deliberately patch title generation so this slice cannot make
an auxiliary-model or network request.
"""

from unittest.mock import patch

import pytest

from agent.title_generator import auto_title_session, maybe_auto_title
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def test_title_is_not_started_until_the_first_exchange_succeeds(db):
    """A user-only turn may still fail or be cancelled, so it is not titleable.

    The origin lifecycle dispatched auto-title only after ``final_response``.
    Keeping the session untitled at turn start is therefore what guarantees
    that failed and cancelled turns leave no title behind.
    """
    db.create_session("active", "cli")
    user_only_history = [{"role": "user", "content": "Repair title lifecycle"}]

    with patch("agent.title_generator.auto_title_session") as generate:
        maybe_auto_title(
            db,
            "active",
            "Repair title lifecycle",
            conversation_history=user_only_history,
        )

    assert db.get_session_title("active") is None
    generate.assert_not_called()


def test_empty_and_failed_generation_leave_the_session_untitled(db):
    """Empty input and a failed auxiliary generation must not persist a title."""
    db.create_session("empty", "cli")
    db.create_session("failed", "cli")

    with patch("agent.title_generator.auto_title_session") as generate:
        maybe_auto_title(db, "empty", "", conversation_history=[])
    generate.assert_not_called()
    assert db.get_session_title("empty") is None

    with patch("agent.title_generator.generate_title", return_value=None):
        auto_title_session(db, "failed", "Generate a useful title")
    assert db.get_session_title("failed") is None


def test_successful_title_is_once_only_and_persisted_before_update(db):
    """Persist exactly once, then notify observers from committed session state."""
    db.create_session("successful", "cli")
    observed = []

    def observe(title, source):
        observed.append(
            (
                title,
                source,
                db.get_session_title("successful"),
                db.get_session_title_source("successful"),
            )
        )

    with patch(
        "agent.title_generator.generate_title", return_value="Title Lifecycle Contract"
    ) as generate:
        auto_title_session(
            db,
            "successful",
            "Repair title lifecycle",
            title_callback=observe,
        )
        auto_title_session(
            db,
            "successful",
            "A later turn must not rename it",
            title_callback=observe,
        )

    generate.assert_called_once()
    assert observed == [
        (
            "Title Lifecycle Contract",
            "llm",
            "Title Lifecycle Contract",
            "llm",
        )
    ]


def test_manual_title_wins_over_late_automatic_completion(db):
    """A manual update racing an automatic result keeps higher authority."""
    db.create_session("manual", "cli")

    def finish_after_manual_update(*_args, **_kwargs):
        db.set_session_title("manual", "Pinned Manual Title")
        return "Late Automatic Title"

    with patch(
        "agent.title_generator.generate_title", side_effect=finish_after_manual_update
    ):
        auto_title_session(db, "manual", "Repair title lifecycle")

    assert db.get_session_title("manual") == "Pinned Manual Title"
    assert db.get_session_title_source("manual") == "user"


def test_title_updates_are_isolated_to_the_target_session(db):
    """Lifecycle state from one session must never title another session."""
    db.create_session("first", "cli")
    db.create_session("second", "cli")

    with patch(
        "agent.title_generator.generate_title", return_value="First Session Only"
    ):
        auto_title_session(db, "first", "Discuss the first session")

    assert db.get_session_title("first") == "First Session Only"
    assert db.get_session_title_source("first") == "llm"
    assert db.get_session_title("second") is None
    assert db.get_session_title_source("second") is None
