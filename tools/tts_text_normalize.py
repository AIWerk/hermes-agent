"""Deterministic cleanup turning assistant Markdown into a spoken script.

Shared by explicit TTS calls, gateway auto-TTS, voice-mode streaming and the web
dashboard. Non-ASCII characters are written as escapes on purpose so the file
stays free of invisible/look-alike glyphs.
"""

from __future__ import annotations

import html
import re

# Sentinel appended to former heading lines so smooth_whitespace_for_tts folds the
# heading into the sentence after it ("Weather, it will be sunny") instead of a bare
# "Weather." label.
_HEAD = "\x00"

_MD_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_MD_UNDERSCORE_BOLD_RE = re.compile(r"__(.+?)__", flags=re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", flags=re.DOTALL)
_MD_UNDERSCORE_ITALIC_RE = re.compile(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", flags=re.DOTALL)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", flags=re.DOTALL)
_MD_HEADING_LINE_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", flags=re.MULTILINE)
_MD_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", flags=re.MULTILINE)
_MD_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", flags=re.MULTILINE)
_MD_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$", flags=re.MULTILINE)
_MD_TABLE_PIPE_RE = re.compile(r"\s*\|\s*")
_URL_RE = re.compile(r"https?://\S+")

_DEGREE_UNITS = (("C", "Celsius"), ("F", "Fahrenheit"))
# Unit suffix (regex, after a digit) -> spoken word; km/h variants before the bare "m".
_UNIT_WORDS = (
    (r"km\s*/\s*h", "kilometres per hour"), (r"km/h", "kilometres per hour"),
    (r"mm", "millimetres"), (r"cm", "centimetres"), (r"m", "metres"))
# Currency prefix (regex) -> spoken word; order matters (NZ$/A$/US$ before bare $).
_CURRENCY_WORDS = (
    (r"NZ\$", "New Zealand dollars", re.IGNORECASE), (r"A\$", "Australian dollars", re.IGNORECASE),
    (r"US\$", "US dollars", re.IGNORECASE), ("€", "euros", 0), ("£", "pounds", 0), (r"\$", "dollars", 0),
)

# Broad emoji / pictograph cleanup: most voice providers read emojis as awkward labels.
_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF☀-➿]+",
    flags=re.UNICODE)
_VARIATION_SELECTOR_RE = re.compile("[︎️]")

_HU_SMALL_NUMBERS = {
    0: "nulla", 1: "egy", 2: "kettő", 3: "három", 4: "négy", 5: "öt", 6: "hat", 7: "hét",
    8: "nyolc", 9: "kilenc", 10: "tíz", 11: "tizenegy", 12: "tizenkettő", 13: "tizenhárom",
    14: "tizennégy", 15: "tizenöt", 16: "tizenhat", 17: "tizenhét", 18: "tizennyolc",
    19: "tizenkilenc", 20: "húsz", 30: "harminc", 40: "negyven", 50: "ötven",
    60: "hatvan", 70: "hetven", 80: "nyolcvan", 90: "kilencven",
}
_DE_SMALL_NUMBERS = {
    0: "null", 1: "eins", 2: "zwei", 3: "drei", 4: "vier", 5: "fünf", 6: "sechs",
    7: "sieben", 8: "acht", 9: "neun", 10: "zehn", 11: "elf", 12: "zwölf",
    13: "dreizehn", 14: "vierzehn", 15: "fünfzehn", 16: "sechzehn", 17: "siebzehn",
    18: "achtzehn", 19: "neunzehn", 20: "zwanzig", 30: "dreißig", 40: "vierzig",
    50: "fünfzig", 60: "sechzig", 70: "siebzig", 80: "achtzig", 90: "neunzig",
}
_EN_SMALL_NUMBERS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
    13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen",
    18: "eighteen", 19: "nineteen", 20: "twenty", 30: "thirty", 40: "forty",
    50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety",
}
_TTS_NUMERIC_TOKEN_RE = re.compile(
    r"(?<![\w/])-?(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[,.]\d+)?)(?![\w/])"
)


def _hungarian_int_to_words(value: int) -> str:
    if value < 0:
        return "mínusz " + _hungarian_int_to_words(abs(value))
    if value in _HU_SMALL_NUMBERS:
        return _HU_SMALL_NUMBERS[value]
    if value < 100:
        tens, ones = value // 10 * 10, value % 10
        return ("huszon" if tens == 20 else _HU_SMALL_NUMBERS[tens]) + _HU_SMALL_NUMBERS[ones]
    if value < 1000:
        hundreds, rest = value // 100, value % 100
        prefix = "száz" if hundreds == 1 else _hungarian_int_to_words(hundreds) + "száz"
        return prefix if rest == 0 else prefix + _hungarian_int_to_words(rest)
    for scale, singular in ((1_000_000_000, "milliárd"), (1_000_000, "millió"), (1000, "ezer")):
        if value >= scale:
            count, rest = value // scale, value % scale
            prefix = singular if scale == 1000 and count == 1 else ("egy" + singular if count == 1 else _hungarian_int_to_words(count) + singular)
            return prefix if rest == 0 else prefix + "-" + _hungarian_int_to_words(rest)
    return str(value)


def _split_decimal_de_hu(value: str) -> tuple[str, str | None]:
    grouped = re.sub(r"(?<=\d)\.(?=\d)", "", value)
    if "," in grouped:
        return grouped.split(",", 1)
    return grouped, None


def _hungarian_number_to_words(raw: str) -> str:
    sign = ""
    value = raw.strip()
    if value.startswith("-"):
        sign, value = "mínusz ", value[1:]
    left, right = _split_decimal_de_hu(value.replace(" ", ""))
    if right is not None:
        right_words = " ".join(_hungarian_int_to_words(int(ch)) for ch in right if ch.isdigit())
        return f"{sign}{_hungarian_int_to_words(int(left or '0'))} egész {right_words}".strip()
    return sign + _hungarian_int_to_words(int(left or "0"))


def _german_int_to_words(value: int) -> str:
    if value < 0:
        return "minus " + _german_int_to_words(abs(value))
    if value in _DE_SMALL_NUMBERS:
        return _DE_SMALL_NUMBERS[value]
    if value < 100:
        tens, ones = value // 10 * 10, value % 10
        return ("ein" if ones == 1 else _DE_SMALL_NUMBERS[ones]) + "und" + _DE_SMALL_NUMBERS[tens]
    if value < 1000:
        hundreds, rest = value // 100, value % 100
        prefix = "einhundert" if hundreds == 1 else _german_int_to_words(hundreds) + "hundert"
        return prefix if rest == 0 else prefix + _german_int_to_words(rest)
    if value < 1_000_000:
        thousands, rest = value // 1000, value % 1000
        prefix = "eintausend" if thousands == 1 else _german_int_to_words(thousands) + "tausend"
        return prefix if rest == 0 else prefix + _german_int_to_words(rest)
    for scale, singular, plural in ((1_000_000_000, "eine Milliarde", " Milliarden"), (1_000_000, "eine Million", " Millionen")):
        if value >= scale:
            count, rest = value // scale, value % scale
            prefix = singular if count == 1 else _german_int_to_words(count) + plural
            return prefix if rest == 0 else prefix + " " + _german_int_to_words(rest)
    return str(value)


def _german_number_to_words(raw: str) -> str:
    sign = ""
    value = raw.strip()
    if value.startswith("-"):
        sign, value = "minus ", value[1:]
    left, right = _split_decimal_de_hu(value.replace(" ", ""))
    if right is not None:
        right_words = " ".join(_german_int_to_words(int(ch)) for ch in right if ch.isdigit())
        return f"{sign}{_german_int_to_words(int(left or '0'))} Komma {right_words}".strip()
    return sign + _german_int_to_words(int(left or "0"))


def _english_int_to_words(value: int) -> str:
    if value < 0:
        return "minus " + _english_int_to_words(abs(value))
    if value in _EN_SMALL_NUMBERS:
        return _EN_SMALL_NUMBERS[value]
    if value < 100:
        tens, ones = value // 10 * 10, value % 10
        return _EN_SMALL_NUMBERS[tens] + ("-" + _EN_SMALL_NUMBERS[ones] if ones else "")
    if value < 1000:
        hundreds, rest = value // 100, value % 100
        prefix = _EN_SMALL_NUMBERS[hundreds] + " hundred"
        return prefix if rest == 0 else prefix + " " + _english_int_to_words(rest)
    for scale, name in ((1_000_000_000, "billion"), (1_000_000, "million"), (1000, "thousand")):
        if value >= scale:
            count, rest = value // scale, value % scale
            prefix = _english_int_to_words(count) + " " + name
            return prefix if rest == 0 else prefix + " " + _english_int_to_words(rest)
    return str(value)


def _split_decimal_en(value: str) -> tuple[str, str | None]:
    grouped = re.sub(r"(?<=\d),(?=\d)", "", value)
    if "." in grouped:
        return grouped.split(".", 1)
    return grouped, None


def _english_number_to_words(raw: str) -> str:
    sign = ""
    value = raw.strip()
    if value.startswith("-"):
        sign, value = "minus ", value[1:]
    left, right = _split_decimal_en(value.replace(" ", ""))
    if right is not None:
        right_words = " ".join(_english_int_to_words(int(ch)) for ch in right if ch.isdigit())
        return f"{sign}{_english_int_to_words(int(left or '0'))} point {right_words}".strip()
    return sign + _english_int_to_words(int(left or "0"))


def _normalize_text_for_tts(text: str, language: str | None = None) -> str:
    if not text:
        return text
    lang = (language or "").strip().lower()
    normalized = str(text)
    if lang.startswith("de"):
        replacements = [
            (r"\s*°\s*C\b", " Grad Celsius"), (r"\s*℃", " Grad Celsius"),
            (r"\s*%", " Prozent"), (r"\s*€|\s*\bEUR\b", " Euro"),
            (r"\s*\bCHF\b", " Schweizer Franken"), (r"\s*\bHUF\b", " Ungarische Forint"),
        ]
        number_words = _german_number_to_words
    elif lang.startswith("hu"):
        replacements = [
            (r"\s*°\s*C\b", " Celsius fok"), (r"\s*℃", " Celsius fok"),
            (r"\s*%", " százalék"), (r"\s*€|\s*\bEUR\b", " euró"),
            (r"\s*\bCHF\b", " svájci frank"), (r"\s*\b(?:HUF|Ft)\b", " forint"),
        ]
        number_words = _hungarian_number_to_words
    elif lang.startswith("en"):
        replacements = []
        number_words = _english_number_to_words
    else:
        return text
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    normalized = _TTS_NUMERIC_TOKEN_RE.sub(lambda match: number_words(match.group(0)), normalized)
    return re.sub(r"\s{2,}", " ", normalized).strip()


def strip_markdown_for_tts(text: str) -> str:
    """Strip Markdown/Telegram formatting while preserving readable words."""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = _MD_CODE_BLOCK_RE.sub(" ", text)
    text = _MD_IMAGE_RE.sub(lambda m: f" {m.group(1)} " if m.group(1) else " ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_UNDERSCORE_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_UNDERSCORE_ITALIC_RE.sub(r"\1", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    # Mark headings (do not just delete the marker): see _HEAD.
    text = _MD_HEADING_LINE_RE.sub(lambda m: m.group(1).rstrip() + _HEAD, text)
    text = _MD_BLOCKQUOTE_RE.sub("", text)
    text = _MD_LIST_ITEM_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)
    # Leftover table pipes become pauses instead of a spoken "vertical bar".
    return _MD_TABLE_PIPE_RE.sub("; ", text)


def _normalize_temperature_ranges(text: str) -> str:
    """``11-17°C`` -> ``11 to 17 degrees Celsius`` (en/em dash or hyphen; unicode minus normalized)."""
    number = r"([-+\u2212]?\d+(?:\.\d+)?)"
    for unit, word in _DEGREE_UNITS:
        text = re.sub(
            r"(?<!\w)" + number + r"\s*[\u2013\u2014-]\s*" + number + r"\s*°\s*" + unit + r"\b",
            lambda m, w=word: (
                f"{m.group(1).replace(chr(0x2212), '-')} to {m.group(2).replace(chr(0x2212), '-')} degrees {w}"
            ),
            text, flags=re.IGNORECASE)
    return text


def normalize_symbols_for_tts(text: str) -> str:
    """Expand common symbols/shorthand into words a TTS engine reads well."""
    if not text:
        return ""
    text = re.sub("[   ]", " ", str(text))  # non-breaking / thin spaces
    text = text.replace("\u2212", "-").replace("…", "...")  # minus sign, ellipsis
    text = _normalize_temperature_ranges(text)
    # Temperatures with a number first, then bare units ("measured in degrees C"),
    # then any remaining degree symbol (angles, stray cases).
    for unit, word in _DEGREE_UNITS:
        text = re.sub(
            r"(?<!\w)([-+]?\d+(?:\.\d+)?)\s*°\s*" + unit + r"\b", r"\1 degrees " + word, text, flags=re.IGNORECASE,
        )
    for unit, word in _DEGREE_UNITS:
        text = re.sub(r"°\s*" + unit + r"\b", "degrees " + word, text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\w)([-+]?\d+(?:\.\d+)?)\s*°", r"\1 degrees", text).replace("°", " degrees")
    # Common weather/travel units.
    for pattern, word in _UNIT_WORDS:
        text = re.sub(r"(?<=\d)\s*" + pattern + r"\b", " " + word, text, flags=re.IGNORECASE)
    # Numeric rates only ("5/month" -> "5 per month").  Requiring digit-then-letter
    # keeps "and/or", "N/A", "TCP/IP" and dates like "2026/06" intact.
    text = re.sub(r"(?<=\d)\s*/\s*(?=[A-Za-z])", " per ", text)
    # Money and percentages. The integer part must END in a digit so a trailing
    # comma ("A$50, ...") is not swallowed into the spoken amount. Prefixed
    # currencies run first so "$" doesn't eat "NZ$".
    for symbol, word, flags in _CURRENCY_WORDS:
        text = re.sub(symbol + r"\s*([\d,]*\d(?:\.\d+)?)", r"\1 " + word, text, flags=flags)
    text = re.sub(r"(?<=\d)\s*%", " percent", text)
    # Operators and separators that commonly leak from formatted answers.
    text = re.sub("[•◦▪▫]", " ", text.replace("&", " and "))  # bullet glyphs
    for symbol, word in (("→", " to "), ("⇒", " to "), ("≈", " about "), ("~", " about ")):
        text = text.replace(symbol, word)
    return _EMOJI_RE.sub("", _VARIATION_SELECTOR_RE.sub("", text))


def smooth_whitespace_for_tts(text: str) -> str:
    """Collapse visual formatting into calm spoken paragraphs. A _HEAD-marked heading folds into
    the next content line as a lead-in ("Weather, It will be sunny."); a heading with no content
    after it becomes its own sentence."""
    if not text:
        return ""
    raw_lines = text.splitlines()
    add_sentence_pauses = sum(1 for raw_line in raw_lines if raw_line.replace(_HEAD, "").strip()) > 1
    lines: list[str] = []
    pending_heading: str | None = None

    def flush_pending() -> None:
        nonlocal pending_heading
        if pending_heading is not None:
            lines.append(pending_heading.rstrip(".:;,") + ".")
            pending_heading = None
    for raw_line in raw_lines:
        is_heading = raw_line.rstrip().endswith(_HEAD)
        line = raw_line.replace(_HEAD, "").strip()
        if not line:
            # Hold a pending heading across blank lines so it still folds into the next content line.
            if pending_heading is None and lines and lines[-1] != "":
                lines.append("")
            continue
        if is_heading:
            flush_pending()
            pending_heading = line.rstrip(".:;,")
            continue
        if pending_heading is not None:
            line = f"{pending_heading.rstrip('.:;,')}, {line}"
            pending_heading = None
        if add_sentence_pauses and line[-1] not in ".!?;:":
            line += "."
        lines.append(line)
    flush_pending()
    text = "\n".join(lines)
    for pattern, repl in ((r"\n{3,}", "\n\n"), (r"[ \t]{2,}", " "), (r"\s+([,.;:!?])", r"\1"),
                          (r"([,.;:!?])([A-Za-z])", r"\1 \2"), (r"\.{4,}", "...")):
        text = re.sub(pattern, repl, text)
    return text.strip()


# ``/reasoning show`` emits ``<think>...</think>`` in the final message: users want to
# SEE reasoning, not hear it. An unterminated block (streaming cut-off) is also silenced.
# Reasoning blocks: models with ``/reasoning show`` enabled emit ``<think>...</think>`` blocks in the final
# assistant message. See #34213.
_THINK_BLOCK_RE = re.compile(r"<think[\s>].*?</think>", flags=re.DOTALL | re.IGNORECASE)
_THINK_BLOCK_OPEN_RE = re.compile(r"<think[\s>].*\Z", flags=re.DOTALL | re.IGNORECASE)

# run_agent.py's turn-end file-mutation verifier footer (a ``⚠️ File-mutation verifier:``
# header line plus indented ``•`` bullets) is a UI affordance, not speech.
_VERIFIER_FOOTER_RE = re.compile(r"^\s*⚠️?\s*File-mutation verifier:.*(?:\n[ \t]+•.*)*", flags=re.MULTILINE)


def strip_nonspoken_blocks(text: str) -> str:
    """Remove ``<think>`` reasoning blocks and the file-mutation verifier footer."""
    if not text:
        return ""
    for pattern in (_THINK_BLOCK_RE, _THINK_BLOCK_OPEN_RE, _VERIFIER_FOOTER_RE):
        text = pattern.sub(" ", text)
    return text


def flatten_newlines_for_payload(text: str) -> str:
    """Collapse newlines into sentence breaks for single-line TTS payloads: some OpenAI-compatible
    backends (e.g. Kokoro) truncate at the first newline; smoothing already ends each line with
    punctuation, so this is safe.

    See #9004.
    """
    if not text:
        return ""
    for pattern, repl in ((r"\n{2,}", ". "), (r"(?<=[.!?;:,])\n", " "), (r"\n", ". "), (r"\.\s*\.", "."),
                          (r"[ \t]{2,}", " ")):
        text = re.sub(pattern, repl, text)
    return text.strip()


def prepare_spoken_text(text: str, max_chars: int | None = 4000) -> str:
    """Return a TTS-friendly script from assistant text (deterministic cleanup, not a rewrite).
    Pipeline: non-spoken blocks > Markdown > symbols/units > line formatting into sentence
    pauses > single line (for newline-sensitive providers), then ``max_chars``."""
    spoken = text
    for step in (strip_nonspoken_blocks, strip_markdown_for_tts, normalize_symbols_for_tts,
                 smooth_whitespace_for_tts, flatten_newlines_for_payload):
        spoken = step(spoken)
    if max_chars is not None and max_chars > 0 and len(spoken) > max_chars:
        spoken = spoken[:max_chars].rstrip()
    return spoken


def _strip_markdown_for_tts(text: str) -> str:
    """``prepare_spoken_text`` without a length cap (``tts_tool`` compatibility name)."""
    return prepare_spoken_text(text, max_chars=None)
