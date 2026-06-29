"""Gateway STT config tests — honor stt.enabled: false from config.yaml."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def test_gateway_config_stt_disabled_from_dict_nested():
    config = GatewayConfig.from_dict({"stt": {"enabled": False}})
    assert config.stt_enabled is False


def test_load_gateway_config_bridges_stt_enabled_from_config_yaml(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"stt": {"enabled": False}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config = load_gateway_config()

    assert config.stt_enabled is False


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_surfaces_path_when_stt_disabled():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)
    runner._has_setup_skill = lambda: True  # Should NOT be consulted in disabled branch.

    with patch(
        "tools.transcription_tools.transcribe_audio",
        side_effect=AssertionError("transcribe_audio should not be called when STT is disabled"),
    ), patch(
        "gateway.run._probe_audio_duration",
        new=AsyncMock(return_value="0:12"),
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "/tmp/voice.ogg" in result
    assert "voice message" in result.lower()
    assert "(duration: 0:12)" in result
    assert "caption" in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_omits_duration_on_probe_failure():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=False)

    with patch(
        "gateway.run._probe_audio_duration",
        new=AsyncMock(return_value=None),
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "",
            ["/tmp/voice.ogg"],
        )

    assert "/tmp/voice.ogg" in result
    assert "duration" not in result.lower()
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_avoids_bogus_no_provider_message_for_backend_key_errors():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": False, "error": "VOICE_TOOLS_OPENAI_KEY not set"},
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "caption",
            ["/tmp/voice.ogg"],
        )

    assert "No STT provider is configured" not in result
    assert "trouble transcribing" in result
    assert "caption" in result
    assert transcripts == []


@pytest.mark.asyncio
async def test_enrich_message_with_transcription_returns_tuple_for_empty_content_placeholder():
    """A successful transcription whose caption is the empty-content placeholder
    must still return the ``(text, transcripts)`` tuple.

    The Discord adapter delivers a captionless voice note as the literal
    ``"(The user sent a message with no text content)"`` placeholder. When STT
    succeeds we strip that redundant placeholder and return just the transcript
    prefix — but the method's contract (and every caller, which unpacks the
    result as ``text, transcripts = ...``) requires a 2-tuple. Returning a bare
    string here raised ``ValueError: too many values to unpack`` and dropped the
    whole voice message on the floor.
    """
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    runner._has_setup_skill = lambda: False

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "hello from a captionless voice note",
            "provider": "local_command",
        },
    ):
        result, transcripts = await runner._enrich_message_with_transcription(
            "(The user sent a message with no text content)",
            ["/tmp/voice.ogg"],
        )

    # The redundant placeholder is stripped, leaving only the transcript prefix.
    assert "hello from a captionless voice note" in result
    assert "(The user sent a message with no text content)" not in result
    # Crucially, the transcripts are still surfaced so callers can echo them.
    assert transcripts == ["hello from a captionless voice note"]


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_transcribes_queued_voice_event():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True)
    fresh_adapter = MagicMock()
    fresh_adapter.platform = Platform.TELEGRAM
    fresh_adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: fresh_adapter}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    runner._thread_metadata_for_source = lambda *a, **k: None
    runner._reply_anchor_for_event = lambda *a, **k: None

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=["/tmp/queued-voice.ogg"],
        media_types=["audio/ogg"],
    )

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "queued voice transcript",
            "provider": "local_command",
        },
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "queued voice transcript" in result
    assert "voice message" in result.lower()
    # Fresh-message path with echo_transcript off (default) must NOT echo the
    # raw transcript back to the chat.
    fresh_adapter.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Voice-transcript privacy: raw transcript must NOT be echoed back to the chat
# on any voice-input path unless echo_transcript is explicitly enabled.
# ---------------------------------------------------------------------------


def test_gateway_config_echo_transcript_defaults_false():
    assert GatewayConfig().echo_transcript is False


def test_gateway_config_echo_transcript_from_dict_flat_and_nested():
    assert GatewayConfig.from_dict({"echo_transcript": True}).echo_transcript is True
    assert GatewayConfig.from_dict({"voice": {"echo_transcript": True}}).echo_transcript is True
    # Absent in both forms -> default False.
    assert GatewayConfig.from_dict({}).echo_transcript is False


def test_gateway_config_echo_transcript_roundtrip():
    cfg = GatewayConfig(echo_transcript=True)
    assert GatewayConfig.from_dict(cfg.to_dict()).echo_transcript is True


def test_load_gateway_config_bridges_echo_transcript_from_config_yaml(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.dump({"voice": {"echo_transcript": True}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config = load_gateway_config()

    assert config.echo_transcript is True


def _voice_drain_runner(adapter, *, echo_transcript: bool):
    """A GatewayRunner wired for the queued/interrupt voice-drain path."""
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(stt_enabled=True, echo_transcript=echo_transcript)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._has_setup_skill = lambda: False
    return runner


def _voice_drain_pieces():
    """Build (adapter, source, session_key) for a pending voice event."""
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_type="dm",
    )
    event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=["/tmp/queued-voice.ogg"],
        media_types=["audio/ogg"],
    )
    adapter = MagicMock()
    adapter.platform = Platform.TELEGRAM
    adapter.get_pending_message = MagicMock(return_value=event)
    adapter.send = AsyncMock()
    return adapter, source


@pytest.mark.asyncio
async def test_drain_path_does_not_echo_transcript_when_flag_off():
    """The queued/interrupt drain path must NOT send the raw transcript to the
    adapter when echo_transcript is off (the default) — this is the privacy
    hole the fix closes. The transcript still reaches the agent via the
    returned enriched text."""
    adapter, source = _voice_drain_pieces()
    runner = _voice_drain_runner(adapter, echo_transcript=False)

    secret = "my bank pin is 1234 transfer the money now"
    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": secret,
            "provider": "local_command",
        },
    ):
        result = await runner._dequeue_pending_with_transcription(
            adapter, "telegram:123", source,
        )

    # Agent still receives the dictation as enriched text.
    assert result is not None
    assert secret in result
    # But NOTHING is echoed back to the chat — no '🎙️ "..."' send at all.
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_path_echoes_transcript_only_when_flag_on():
    adapter, source = _voice_drain_pieces()
    runner = _voice_drain_runner(adapter, echo_transcript=True)

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={
            "success": True,
            "transcript": "hello there",
            "provider": "local_command",
        },
    ):
        await runner._dequeue_pending_with_transcription(
            adapter, "telegram:123", source,
        )

    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert sent_text == '🎙️ "hello there"'


@pytest.mark.asyncio
async def test_maybe_echo_transcripts_gated_by_flag():
    """The centralized echo helper is the single privacy gate for every
    voice-input echo site."""
    from gateway.run import GatewayRunner

    adapter = MagicMock()
    adapter.send = AsyncMock()

    runner_off = GatewayRunner.__new__(GatewayRunner)
    runner_off.config = GatewayConfig(echo_transcript=False)
    await runner_off._maybe_echo_transcripts(adapter, "c1", ["secret dictation"], None)
    adapter.send.assert_not_awaited()

    runner_on = GatewayRunner.__new__(GatewayRunner)
    runner_on.config = GatewayConfig(echo_transcript=True)
    await runner_on._maybe_echo_transcripts(adapter, "c1", ["secret dictation"], None)
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[1] == '🎙️ "secret dictation"'
