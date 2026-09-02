import json
from pathlib import Path

import pytest

from tools import tts_tool
from tools.tts_tool import (
    _english_number_to_words,
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


def test_normalize_text_for_tts_spells_hungarian_eur_and_huf_codes():
    text = "Ez 12 EUR és 350 HUF."

    normalized = _normalize_text_for_tts(text, language="hu-HU")

    assert normalized == "Ez tizenkettő euró és háromszázötven forint."


def test_normalize_text_for_tts_spells_german_celsius_percent_and_currency():
    text = "Heute sind 23 °C, Regen 5%, Kosten 12 CHF."

    normalized = _normalize_text_for_tts(text, language="de")

    assert normalized == "Heute sind dreiundzwanzig Grad Celsius, Regen fünf Prozent, Kosten zwölf Schweizer Franken."


def test_normalize_text_for_tts_spells_german_negative_decimal():
    text = "Draussen sind -3,5°C."

    normalized = _normalize_text_for_tts(text, language="de-CH")

    assert normalized == "Draussen sind minus drei Komma fünf Grad Celsius."


def test_locale_specific_number_words_distinguish_thousands_from_decimals():
    assert _hungarian_number_to_words("1.000") == "ezer"
    assert _hungarian_number_to_words("3,14") == "három egész egy négy"
    assert _german_number_to_words("1.000") == "eintausend"
    assert _german_number_to_words("3,14") == "drei Komma eins vier"
    assert _english_number_to_words("1,000") == "one thousand"


def test_locale_specific_number_words_support_multi_group_thousands():
    assert _hungarian_number_to_words("1.000.000") == "egymillió"
    assert _german_number_to_words("1.000.000") == "eine Million"
    assert _english_number_to_words("1,000,000") == "one million"
    assert _hungarian_number_to_words("1.000.000,5") == "egymillió egész öt"
    assert _german_number_to_words("1.000.000,5") == "eine Million Komma fünf"
    assert _english_number_to_words("1,000,000.5") == "one million point five"


def test_normalize_text_for_tts_speaks_multi_group_thousands():
    assert "eine Million" in _normalize_text_for_tts("Das kostet 1.000.000 CHF.", language="de")
    assert "egymillió" in _normalize_text_for_tts("Ez 1.000.000 Ft.", language="hu")
    assert "one million" in _normalize_text_for_tts("It costs 1,000,000 dollars.", language="en")


def test_normalize_text_for_tts_spells_german_eur_and_huf_codes():
    normalized = _normalize_text_for_tts("Kosten 12 EUR und 350 HUF.", language="de-DE")

    assert normalized == "Kosten zwölf Euro und dreihundertfünfzig Ungarische Forint."


def test_normalize_text_for_tts_preserves_non_numeric_text():
    text = "Szia, ez egy próba."

    normalized = _normalize_text_for_tts(text, language="hu")

    assert normalized == text


def _install_fake_selected_provider(monkeypatch, tmp_path, config):
    spoken = {}
    output = tmp_path / "voice.mp3"
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: config)
    monkeypatch.setattr(tts_tool, "_import_elevenlabs", lambda: object)

    def fake_generate(text, file_str, _config):
        spoken["text"] = text
        Path(file_str).write_bytes(b"audio")
        return file_str

    monkeypatch.setattr(tts_tool, "_generate_elevenlabs", fake_generate)
    return spoken, output


@pytest.mark.parametrize("normalize_setting", [True, None])
def test_text_to_speech_tool_sends_normalized_text_to_selected_provider(
    monkeypatch, tmp_path, normalize_setting
):
    config = {
        "provider": "elevenlabs",
        "elevenlabs": {"language_code": "hu"},
    }
    if normalize_setting is not None:
        config["normalize_text"] = normalize_setting
    spoken, output = _install_fake_selected_provider(monkeypatch, tmp_path, config)

    result = json.loads(text_to_speech_tool("Ma 23 °C lesz.", output_path=str(output)))

    assert result["success"] is True, result
    assert result["provider"] == "elevenlabs"
    assert spoken["text"] == "Ma huszonhárom Celsius fok lesz."


def test_text_to_speech_tool_can_disable_normalization(monkeypatch, tmp_path):
    config = {
        "provider": "elevenlabs",
        "normalize_text": False,
        "elevenlabs": {"language_code": "hu"},
    }
    spoken, output = _install_fake_selected_provider(monkeypatch, tmp_path, config)

    result = json.loads(text_to_speech_tool("Ma 23 °C lesz.", output_path=str(output)))

    assert result["success"] is True, result
    assert spoken["text"] == "Ma 23 degrees Celsius lesz."
