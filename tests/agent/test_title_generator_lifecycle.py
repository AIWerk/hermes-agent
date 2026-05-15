"""Tests for three-step session title lifecycle."""

from unittest.mock import MagicMock, patch

import pytest

from agent.title_generator import (
    auto_title_session,
    finalize_session_title,
    maybe_retitle_session,
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
