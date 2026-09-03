"""Tests for three-step session title lifecycle.

Includes the historical AIWerk lifecycle contract ported from origin blob
2c9eaa95817620a88f1586aefa439cfe8460e04c, composed with the current
three-step lifecycle tests. Historical source labels are adapted to today's
``manual`` and ``auto_initial`` metadata vocabulary.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.title_generator import (
    auto_title_session,
    finalize_session_title,
    maybe_auto_title,
    maybe_retitle_session,
    retitle_session,
)
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _messages(user_turns=5):
    messages = []
    for i in range(user_turns):
        messages.append({"role": "user", "content": f"User turn {i}: discuss session notes and title drift."})
        messages.append({"role": "assistant", "content": f"Assistant turn {i}: implementation details."})
    return messages


def test_retitle_session_formats_real_message_content_without_name_errors(db):
    db.create_session("s1", "cli")
    db.set_session_title("s1", "Old title", source="auto_initial", turn_index=1)
    with patch("agent.title_generator._auto_title_enabled", return_value=True), patch(
        "agent.title_generator._call_lifecycle_title_llm", return_value="Runtime Recovery"
    ) as call:
        assert retitle_session(db, "s1", _messages(1), turn_index=6) is True

    prompt = call.call_args.kwargs["user_prompt"]
    assert "user: User turn 0" in prompt
    assert "assistant: Assistant turn 0" in prompt
    assert db.get_session_title_metadata("s1")["title_source"] == "auto_mid"


def test_set_session_title_tracks_source_metadata(db):
    db.create_session("s1", "cli")

    assert db.set_session_title("s1", "Manual Project Title") is True
    meta = db.get_session_title_metadata("s1")
    assert meta["title"] == "Manual Project Title"
    assert meta["title_source"] == "manual"
    assert meta["title_updated_at"] is not None

    assert db.set_session_title("s1", "Auto Project Title", source="auto_mid", turn_index=5) is True
    meta = db.get_session_title_metadata("s1")
    assert meta["title"] == "Auto Project Title"
    assert meta["title_source"] == "auto_mid"
    assert meta["title_turn_index"] == 5


def test_initial_auto_title_sets_auto_initial_source(db):
    db.create_session("s1", "cli")

    with patch("agent.title_generator.generate_title", return_value="Hermes Session Notes"):
        auto_title_session(db, "s1", "start", "done")

    meta = db.get_session_title_metadata("s1")
    assert meta["title"] == "Hermes Session Notes"
    assert meta["title_source"] == "auto_initial"
    assert meta["title_turn_index"] == 1


def test_mid_session_retitle_never_overwrites_manual_title(db):
    db.create_session("s1", "cli")
    db.set_session_title("s1", "My Manual Title")

    with patch("agent.title_generator.generate_retitle", return_value="Better Auto Title") as gen:
        changed = maybe_retitle_session(db, "s1", _messages(), turn_index=10, synchronous=True)

    assert changed is False
    gen.assert_not_called()
    assert db.get_session_title("s1") == "My Manual Title"


def test_mid_session_retitle_updates_drifted_auto_title(db):
    db.create_session("s1", "cli")
    db.set_session_title("s1", "Bundled Skill Policy", source="auto_initial", turn_index=1)

    with patch("agent.title_generator.generate_retitle", return_value="Hermes Runtime Session Notes"):
        changed = maybe_retitle_session(db, "s1", _messages(), turn_index=6, synchronous=True)

    assert changed is True
    meta = db.get_session_title_metadata("s1")
    assert meta["title"] == "Hermes Runtime Session Notes"
    assert meta["title_source"] == "auto_mid"
    assert meta["title_turn_index"] == 6


def test_mid_session_retitle_is_throttled(db):
    db.create_session("s1", "cli")
    db.set_session_title("s1", "Initial Auto", source="auto_initial", turn_index=4)

    with patch("agent.title_generator.generate_retitle", return_value="New Auto") as gen:
        changed = maybe_retitle_session(db, "s1", _messages(), turn_index=6, synchronous=True)

    assert changed is False
    gen.assert_not_called()
    assert db.get_session_title("s1") == "Initial Auto"


def test_final_title_refinement_updates_auto_title_but_not_manual(db):
    summary = {
        "short_summary": "Implemented bundled skill policy and runtime session notes.",
        "outline": ["Policy", "Runtime notes", "Retitle lifecycle"],
        "topics": ["hermes", "session-notes"],
    }

    db.create_session("auto", "cli")
    db.set_session_title("auto", "Bundled Skill Policy", source="auto_mid", turn_index=6)
    with patch("agent.title_generator.generate_final_title", return_value="Runtime Session Notes Lifecycle"):
        assert finalize_session_title(db, "auto", summary) is True
    assert db.get_session_title_metadata("auto")["title_source"] == "auto_final"

    db.create_session("manual", "cli")
    db.set_session_title("manual", "Pinned Manual Title")
    with patch("agent.title_generator.generate_final_title", return_value="Should Not Apply") as gen:
        assert finalize_session_title(db, "manual", summary) is False
    gen.assert_not_called()
    assert db.get_session_title("manual") == "Pinned Manual Title"


def test_session_summarizer_final_flag_invokes_title_refinement():
    from agent.session_summarizer import update_session_summary

    fake_db = MagicMock()
    fake_db.get_messages.return_value = [{"role": "user", "content": "hello"}]
    fake_db.get_session_title.return_value = "Old Auto"
    fake_db.set_session_summary.return_value = True

    with patch(
        "agent.session_summarizer.generate_session_summary",
        return_value={
            "short_summary": "A summary",
            "outline": ["one"],
            "topics": ["topic"],
            "model": "m",
        },
    ), patch("agent.title_generator.finalize_session_title", return_value=True) as finalize:
        assert update_session_summary(fake_db, "s1", final_title_refinement=True) is True

    finalize.assert_called_once()


def test_title_is_not_started_until_the_first_exchange_succeeds(db):
    """A user-only turn may still fail or be cancelled, so it is not titleable."""
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
    """Empty input and failed auxiliary generation must not persist a title."""
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
            "auto_initial",
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
    assert db.get_session_title_source("manual") == "manual"


def test_title_updates_are_isolated_to_the_target_session(db):
    """Lifecycle state from one session must never title another session."""
    db.create_session("first", "cli")
    db.create_session("second", "cli")

    with patch(
        "agent.title_generator.generate_title", return_value="First Session Only"
    ):
        auto_title_session(db, "first", "Discuss the first session")

    assert db.get_session_title("first") == "First Session Only"
    assert db.get_session_title_source("first") == "auto_initial"
    assert db.get_session_title("second") is None
    assert db.get_session_title_source("second") is None
