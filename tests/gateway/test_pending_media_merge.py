"""Pending gateway media merge isolation and idempotency tests."""

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event
from gateway.session import SessionSource


def _event(
    message_type,
    path,
    text="",
    *,
    chat_id="12345",
    thread_id=None,
    user_id="u1",
    message_id=None,
    update_id=None,
):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        thread_id=thread_id,
        user_id=user_id,
    )
    media_type = "audio/ogg" if message_type == MessageType.VOICE else "image/jpeg"
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source,
        media_urls=[path],
        media_types=[media_type],
        message_id=message_id,
        platform_update_id=update_id,
    )


def test_pending_photo_media_merges_album_burst():
    pending = {}

    merge_pending_message_event(
        pending,
        "s",
        _event(MessageType.PHOTO, "/tmp/a.jpg", "a", message_id="m1"),
    )
    merge_pending_message_event(
        pending,
        "s",
        _event(MessageType.PHOTO, "/tmp/b.jpg", "b", message_id="m2"),
    )

    event = pending["s"]
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls == ["/tmp/a.jpg", "/tmp/b.jpg"]
    assert "a" in event.text
    assert "b" in event.text


def test_pending_voice_followup_preserves_both_user_turns_once():
    pending = {}

    merge_pending_message_event(
        pending,
        "s",
        _event(MessageType.VOICE, "/tmp/old.ogg", "old", message_id="m1"),
    )
    merge_pending_message_event(
        pending,
        "s",
        _event(MessageType.VOICE, "/tmp/new.ogg", "new", message_id="m2"),
    )

    event = pending["s"]
    assert event.message_type == MessageType.VOICE
    assert event.media_urls == ["/tmp/old.ogg", "/tmp/new.ogg"]
    assert event.media_types == ["audio/ogg", "audio/ogg"]
    assert "old" in event.text
    assert "new" in event.text


def test_replayed_server_event_is_merged_exactly_once():
    pending = {}
    original = _event(
        MessageType.VOICE,
        "/tmp/voice.ogg",
        "voice text",
        message_id="m1",
        update_id=41,
    )
    replay = _event(
        MessageType.VOICE,
        "/tmp/voice.ogg",
        "voice text",
        message_id="m1",
        update_id=41,
    )

    merge_pending_message_event(pending, "s", original)
    merge_pending_message_event(pending, "s", replay)

    event = pending["s"]
    assert event.media_urls == ["/tmp/voice.ogg"]
    assert event.media_types == ["audio/ogg"]
    assert event.text == "voice text"


def test_replayed_server_event_after_other_media_is_merged_exactly_once():
    pending = {}
    first = _event(
        MessageType.PHOTO,
        "/tmp/first.jpg",
        "first",
        message_id="m1",
        update_id=41,
    )
    second = _event(
        MessageType.PHOTO,
        "/tmp/second.jpg",
        "second",
        message_id="m2",
        update_id=42,
    )
    second_replay = _event(
        MessageType.PHOTO,
        "/tmp/second.jpg",
        "second",
        message_id="m2",
        update_id=42,
    )

    merge_pending_message_event(pending, "s", first)
    merge_pending_message_event(pending, "s", second)
    merge_pending_message_event(pending, "s", second_replay)

    event = pending["s"]
    assert event.media_urls == ["/tmp/first.jpg", "/tmp/second.jpg"]
    assert event.media_types == ["image/jpeg", "image/jpeg"]
    assert event.text.count("second") == 1


@pytest.mark.parametrize(
    ("first_update", "replay_update"),
    [(41, None), (None, 41)],
)
def test_replay_suppression_matches_any_stable_token(
    first_update, replay_update
):
    pending = {}
    first = _event(
        MessageType.VOICE,
        "/tmp/voice.ogg",
        "voice text",
        message_id="m1",
        update_id=first_update,
    )
    replay = _event(
        MessageType.VOICE,
        "/tmp/voice.ogg",
        "voice text",
        message_id="m1",
        update_id=replay_update,
    )

    merge_pending_message_event(pending, "s", first)
    merge_pending_message_event(pending, "s", replay)

    assert pending["s"].media_urls == ["/tmp/voice.ogg"]
    assert pending["s"].text == "voice text"


def test_pending_media_never_merges_across_telegram_actor():
    pending = {}
    merge_pending_message_event(
        pending,
        "shared-session",
        _event(MessageType.PHOTO, "/tmp/u1.jpg", "u1", user_id="u1", message_id="m1"),
    )
    incoming = _event(
        MessageType.PHOTO,
        "/tmp/u2.jpg",
        "u2",
        user_id="u2",
        message_id="m2",
    )

    merge_pending_message_event(pending, "shared-session", incoming)

    assert pending["shared-session"] is incoming
    assert pending["shared-session"].media_urls == ["/tmp/u2.jpg"]
    assert pending["shared-session"].text == "u2"


def test_pending_media_never_merges_across_telegram_chat_or_topic():
    pending = {}
    merge_pending_message_event(
        pending,
        "shared-session",
        _event(
            MessageType.PHOTO,
            "/tmp/topic-a.jpg",
            "topic a",
            chat_id="chat-a",
            thread_id="topic-a",
            message_id="m1",
        ),
    )
    incoming = _event(
        MessageType.PHOTO,
        "/tmp/topic-b.jpg",
        "topic b",
        chat_id="chat-b",
        thread_id="topic-b",
        message_id="m2",
    )

    merge_pending_message_event(pending, "shared-session", incoming)

    assert pending["shared-session"] is incoming
    assert pending["shared-session"].media_urls == ["/tmp/topic-b.jpg"]
    assert pending["shared-session"].source.chat_id == "chat-b"
    assert pending["shared-session"].source.thread_id == "topic-b"
