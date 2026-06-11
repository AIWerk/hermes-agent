from pathlib import Path

from tools import tts_tool
from tools.tts_tool import _normalize_text_for_tts, text_to_speech_tool


def test_normalize_text_for_tts_spells_hungarian_celsius_and_percent():
    text = "Ma 23 °C lesz, holnap 5% eső esély."

    normalized = _normalize_text_for_tts(text, language="hu")

    assert normalized == "Ma huszonhárom Celsius fok lesz, holnap öt százalék eső esély."


def test_normalize_text_for_tts_spells_hungarian_negative_decimal_and_currency():
    text = "Kint -3,5°C van, ez 12 CHF."

    normalized = _normalize_text_for_tts(text, language="hu")

    assert normalized == "Kint mínusz három egész öt Celsius fok van, ez tizenkettő svájci frank."


def test_normalize_text_for_tts_spells_german_celsius_percent_and_currency():
    text = "Heute sind 23 °C, Regen 5%, Kosten 12 CHF."

    normalized = _normalize_text_for_tts(text, language="de")

    assert normalized == "Heute sind dreiundzwanzig Grad Celsius, Regen fünf Prozent, Kosten zwölf Schweizer Franken."


def test_normalize_text_for_tts_spells_german_negative_decimal():
    text = "Draussen sind -3,5°C."

    normalized = _normalize_text_for_tts(text, language="de-CH")

    assert normalized == "Draussen sind minus drei Komma fünf Grad Celsius."


def test_normalize_text_for_tts_preserves_non_numeric_text():
    text = "Szia, ez egy próba."

    normalized = _normalize_text_for_tts(text, language="hu")

    assert normalized == text


def test_text_to_speech_tool_sends_normalized_text_to_provider(monkeypatch, tmp_path):
    spoken = {}
    output = tmp_path / "voice.mp3"

    monkeypatch.setattr(
        tts_tool,
        "_load_tts_config",
        lambda: {
            "provider": "elevenlabs",
            "normalize_text": True,
            "elevenlabs": {"language_code": "hu"},
        },
    )
    monkeypatch.setattr(tts_tool, "_import_elevenlabs", lambda: object)

    def fake_generate(text, file_str, _config):
        spoken["text"] = text
        Path(file_str).write_bytes(b"audio")

    monkeypatch.setattr(tts_tool, "_generate_elevenlabs", fake_generate)

    result = text_to_speech_tool("Ma 23 °C lesz.", output_path=str(output))

    assert '"success": true' in result.lower()
    assert spoken["text"] == "Ma huszonhárom Celsius fok lesz."
