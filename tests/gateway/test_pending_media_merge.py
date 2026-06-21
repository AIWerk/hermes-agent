from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event
from gateway.session import SessionSource


def _event(message_type, path, text=""):
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        user_id="u1",
    )
    media_type = "audio/ogg" if message_type == MessageType.VOICE else "image/jpeg"
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=source,
        media_urls=[path],
        media_types=[media_type],
    )


def test_pending_photo_media_merges_album_burst():
    pending = {}

    merge_pending_message_event(pending, "s", _event(MessageType.PHOTO, "/tmp/a.jpg", "a"))
    merge_pending_message_event(pending, "s", _event(MessageType.PHOTO, "/tmp/b.jpg", "b"))

    event = pending["s"]
    assert event.message_type == MessageType.PHOTO
    assert event.media_urls == ["/tmp/a.jpg", "/tmp/b.jpg"]
    assert "a" in event.text
    assert "b" in event.text


def test_pending_voice_followup_replaces_stale_voice_instead_of_merging():
    pending = {}

    merge_pending_message_event(pending, "s", _event(MessageType.VOICE, "/tmp/old.ogg", "old"))
    merge_pending_message_event(pending, "s", _event(MessageType.VOICE, "/tmp/new.ogg", "new"))

    event = pending["s"]
    assert event.message_type == MessageType.VOICE
    assert event.media_urls == ["/tmp/new.ogg"]
    assert event.media_types == ["audio/ogg"]
    assert event.text == "new"
