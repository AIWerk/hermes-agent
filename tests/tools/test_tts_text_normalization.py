from pathlib import Path

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


def test_multi_group_thousands_separators_read_as_whole_number_de_hu():
    # de/hu use '.' as the thousands separator, so '1.000.000' is one million,
    # not three separate numbers. The tokenizer must capture the whole value.
    assert _hungarian_number_to_words("1.000.000") == "egymillió"
    assert _german_number_to_words("1.000.000") == "eine Million"
    assert (
        _hungarian_number_to_words("12.345.678")
        == "tizenkettőmillió-háromszáznegyvenötezer-hatszázhetvennyolc"
    )
    assert (
        _german_number_to_words("12.345.678")
        == "zwölf Millionen dreihundertfünfundvierzigtausendsechshundertachtundsiebzig"
    )
    # Multi-group + comma decimal stays correct (',' is the de/hu decimal).
    assert _hungarian_number_to_words("1.000.000,5") == "egymillió egész öt"
    assert _german_number_to_words("1.000.000,5") == "eine Million Komma fünf"


def test_multi_group_thousands_separators_read_as_whole_number_en():
    # en uses ',' as the thousands separator and '.' as the decimal point.
    assert _english_number_to_words("1,000,000") == "one million"
    assert (
        _english_number_to_words("12,345,678")
        == "twelve million three hundred forty-five thousand six hundred seventy-eight"
    )
    assert _english_number_to_words("1,000,000.5") == "one million point five"


def test_single_group_thousands_still_correct_after_multi_group_fix():
    # The multi-group extension must not regress the single-group case.
    assert _hungarian_number_to_words("1.000") == "ezer"
    assert _german_number_to_words("1.000") == "eintausend"
    assert _english_number_to_words("1,000") == "one thousand"
    # de/hu decimals (comma) still read as decimals, not thousands.
    assert _hungarian_number_to_words("3,14") == "három egész egy négy"
    assert _german_number_to_words("3,14") == "drei Komma eins vier"


def test_normalize_text_for_tts_speaks_multi_group_thousands():
    de = _normalize_text_for_tts("Das kostet 1.000.000 CHF.", language="de")
    assert "eine Million" in de
    assert "Komma" not in de

    hu = _normalize_text_for_tts("Ez 1.000.000 Ft.", language="hu")
    assert "egymillió" in hu
    assert "egész" not in hu

    en = _normalize_text_for_tts("It costs 1,000,000 dollars.", language="en")
    assert "one million" in en
    assert "point" not in en


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
