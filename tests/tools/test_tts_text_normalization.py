from pathlib import Path

from tools import tts_tool
from tools.tts_tool import (
    _german_number_to_words,
    _hungarian_number_to_words,
    _normalize_text_for_tts,
    text_to_speech_tool,
)


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


def test_hungarian_thousands_separator_is_not_read_as_decimal():
    # In hu, '.' is the thousands separator: 1.000 is one thousand, NOT a
    # decimal ('egy egész nulla nulla nulla' would be the bug).
    assert _hungarian_number_to_words("1.000") == "ezer"
    assert "egész" not in _hungarian_number_to_words("1.000")
    assert _hungarian_number_to_words("12.500") == "tizenkettőezer-ötszáz"
    # Comma stays the decimal separator.
    assert _hungarian_number_to_words("3,14") == "három egész egy négy"


def test_german_thousands_separator_is_not_read_as_decimal():
    # In de, '.' is the thousands separator: 1.000 == eintausend, NOT a decimal.
    assert _german_number_to_words("1.000") == "eintausend"
    assert _german_number_to_words("12.500") == "zwölftausendfünfhundert"
    # Comma stays the decimal separator.
    assert _german_number_to_words("3,14") == "drei Komma eins vier"


def test_normalize_text_for_tts_speaks_thousands_grouping_de_and_hu():
    de = _normalize_text_for_tts("Das kostet 1.000 CHF.", language="de")
    assert "eintausend" in de
    assert "Komma" not in de

    hu = _normalize_text_for_tts("Ez 2.000 Ft.", language="hu")
    assert "kettőezer" in hu
    assert "egész" not in hu


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
