"""Stop-word language heuristic (no dependencies)."""

from __future__ import annotations

import re

STOP_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the and with you we our are will for this that have your".split()),
    "de": frozenset("und der die das mit für wir sie ist nicht auf bei eine oder wird".split()),
    "fr": frozenset("et les des pour avec nous vous est une dans sur aux votre sont".split()),
    "es": frozenset("y los las nosotros del más tu".split()),
    "pt": frozenset("não você uma com os das dos são também sua seu em".split()),
    "nl": frozenset("het een voor wij zijn je van ook bij niet".split()),
    "it": frozenset("il della sono che gli nel delle".split()),
}
_WORD = re.compile(r"[a-zà-ÿа-яё]+")
_CYRILLIC = re.compile(r"[а-яё]")
_LATIN_LETTER = re.compile(r"[a-zà-ÿ]")


def detect_language(text: str, allowed: tuple[str, ...], min_words: int) -> str | None:
    """Return a language code only when it is NOT allowed and clearly dominant."""
    low = text.lower()
    words = _WORD.findall(low)
    if len(words) < min_words:
        return None
    cyr = len(_CYRILLIC.findall(low))
    lat = len(_LATIN_LETTER.findall(low))
    if cyr / max(1, cyr + lat) >= 0.3:
        return None if "ru" in allowed else "ru"
    counts = {lang: sum(1 for w in words if w in sw) for lang, sw in STOP_WORDS.items()}
    lang = max(counts, key=lambda k: counts[k])
    if lang in allowed or counts[lang] == 0:
        return None
    if counts[lang] > 2 * counts["en"]:
        return lang
    return None
