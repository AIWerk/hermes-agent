"""Tests for gateway auto-TTS voice reply audio format selection."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class TestAutoVoiceReplyFormat:
    @pytest.mark.asyncio
    async def test_telegram_auto_voice_reply_requests_ogg_for_native_voice_bubble(self):
        runner = _make_runner()
        adapter = _make_adapter(Platform.TELEGRAM)
        runner.adapters[Platform.TELEGRAM] = adapter
        event = _make_event(
            Platform.TELEGRAM,
            chat_id="telegram-chat",
            thread_id="topic-7",
            user_id="actor-9",
        )
        requested_paths = []

        def fake_tts(*, text, output_path):
            requested_paths.append(output_path)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"fake ogg")
            return json.dumps({
                "success": True,
                "file_path": output_path,
                "provider": "gemini",
                "voice_compatible": True,
            })

        with patch("tools.tts_tool.text_to_speech_tool", side_effect=fake_tts):
            await runner._send_voice_reply(event, "hello from auto tts")

        assert requested_paths == [adapter.send_voice.await_args.kwargs["audio_path"]]
        assert requested_paths[0].endswith(".ogg")
        adapter.send_voice.assert_awaited_once()
        assert adapter.send_voice.await_args.kwargs["chat_id"] == "telegram-chat"
        assert adapter.send_voice.await_args.kwargs["reply_to"] == "456"
        assert adapter.send_voice.await_args.kwargs["metadata"]["thread_id"] == "topic-7"
        assert adapter.send_voice.await_args.kwargs["metadata"]["notify"] is True

    @pytest.mark.asyncio
    async def test_non_telegram_auto_voice_reply_keeps_mp3_default(self):
        runner = _make_runner()
        adapter = _make_adapter(Platform.SLACK)
        runner.adapters[Platform.SLACK] = adapter
        event = _make_event(Platform.SLACK)
        requested_paths = []

        def fake_tts(*, text, output_path):
            requested_paths.append(output_path)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"fake mp3")
            return json.dumps({
                "success": True,
                "file_path": output_path,
                "provider": "gemini",
                "voice_compatible": False,
            })

        with patch("tools.tts_tool.text_to_speech_tool", side_effect=fake_tts):
            await runner._send_voice_reply(event, "hello from auto tts")

        assert requested_paths and requested_paths[0].endswith(".mp3")
        adapter.send_voice.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "send_error",
        [RuntimeError("send failed"), pytest.param("cancel", id="cancelled")],
    )
    async def test_auto_voice_reply_temp_media_is_cleaned_on_send_failure_or_cancel(
        self, send_error
    ):
        runner = _make_runner()
        adapter = _make_adapter(Platform.TELEGRAM)
        if send_error == "cancel":
            adapter.send_voice.side_effect = asyncio.CancelledError()
        else:
            adapter.send_voice.side_effect = send_error
        runner.adapters[Platform.TELEGRAM] = adapter
        event = _make_event(Platform.TELEGRAM)
        requested_paths = []

        def fake_tts(*, text, output_path):
            requested_paths.append(output_path)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"temporary audio")
            return json.dumps({"success": True, "file_path": output_path})

        with patch("tools.tts_tool.text_to_speech_tool", side_effect=fake_tts):
            if send_error == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await runner._send_voice_reply(event, "hello")
            else:
                await runner._send_voice_reply(event, "hello")

        assert requested_paths
        assert not Path(requested_paths[0]).exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "platform",
        [Platform.MATRIX, Platform.FEISHU, Platform.WHATSAPP, Platform.SIGNAL],
    )
    async def test_opus_platform_auto_voice_reply_requests_ogg(self, platform):
        """Every OPUS_VOICE_PLATFORMS member gets an explicit .ogg output path.

        Regression for #14841 (Matrix) / #45557 (Feishu): _send_voice_reply
        hardcoded .ogg for Telegram only, so Matrix/Feishu voice replies were
        synthesized as MP3 and delivered as plain attachments instead of
        native voice bubbles.
        """
        runner = _make_runner()
        adapter = _make_adapter(platform)
        runner.adapters[platform] = adapter
        event = _make_event(platform)
        requested_paths = []

        def fake_tts(*, text, output_path):
            requested_paths.append(output_path)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"fake ogg opus")
            return json.dumps({
                "success": True,
                "file_path": output_path,
                "provider": "gemini",
                "voice_compatible": True,
            })

        with patch("tools.tts_tool.text_to_speech_tool", side_effect=fake_tts):
            await runner._send_voice_reply(event, "hello from auto tts")

        assert requested_paths and requested_paths[0].endswith(".ogg")
        adapter.send_voice.assert_awaited_once()
        assert adapter.send_voice.await_args.kwargs["audio_path"].endswith(".ogg")

    def test_should_send_voice_reply_streamed_global_auto_tts_fires(self):
        """Streamed reply + global voice.auto_tts (no /voice opt-in) sends voice.

        Regression for the #51867/#23983 remainder: when streaming consumed
        the text, the base adapter's auto-TTS gets text_content=None, and the
        runner path used to consult only self._voice_mode — so a chat relying
        purely on the global voice.auto_tts default silently lost its voice
        reply.
        """
        runner = _make_runner()
        adapter = _make_adapter(Platform.TELEGRAM)
        adapter._should_auto_tts_for_chat = MagicMock(return_value=True)
        runner.adapters[Platform.TELEGRAM] = adapter
        voice_event = _make_event(
            Platform.TELEGRAM, chat_id="123", message_type=MessageType.VOICE
        )

        assert runner._should_send_voice_reply(
            voice_event, "hello", [], already_sent=True
        ) is True

    def test_should_send_voice_reply_voice_only_still_requires_voice_input(self):
        """Explicit voice_only must not widen to text input (#73508 regression).

        Persisted voice_only mode is synced into the adapter as an explicit
        auto-TTS opt-in, so adapter_auto_tts is True for this chat. The
        chat-level mode stays authoritative: text input gets no voice reply,
        voice input still does.
        """
        runner = _make_runner()
        runner._voice_mode["telegram:123"] = "voice_only"
        adapter = _make_adapter(Platform.TELEGRAM)
        adapter._should_auto_tts_for_chat = MagicMock(return_value=True)
        runner.adapters[Platform.TELEGRAM] = adapter
        event = _make_event(Platform.TELEGRAM, chat_id="123")

        assert runner._should_send_voice_reply(event, "hello", []) is False

        voice_event = _make_event(Platform.TELEGRAM, chat_id="123", message_type=MessageType.VOICE)
        assert runner._should_send_voice_reply(voice_event, "hello", [], already_sent=True) is True

def _make_runner() -> GatewayRunner:
    with patch("gateway.run.GatewayRunner._load_voice_modes", return_value={}):
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._voice_mode = {}
        runner.adapters = {}
    return runner


def _make_adapter(platform: Platform) -> MagicMock:
    adapter = MagicMock()
    adapter.platform = platform
    adapter.send_voice = AsyncMock()
    return adapter


def _make_event(
    platform: Platform,
    chat_id: str = "123",
    message_type: MessageType = MessageType.TEXT,
    *,
    thread_id: str | None = None,
    user_id: str = "u1",
) -> MessageEvent:
    return MessageEvent(
        text="trigger",
        source=SessionSource(
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            user_name="User",
        ),
        message_type=message_type,
        message_id="456",
    )
