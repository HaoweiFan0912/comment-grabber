""""Must include" keywords: parse what the user typed and check whether a text contains every keyword.

Rules:
- Separate keywords with spaces or commas (, ， 、); quote a phrase that contains spaces, like "battery life".
- Every keyword must appear, in any order and anywhere; they don't need to be next to each other.
- Matching is literal: no translation or synonyms, Simplified and Traditional Chinese are different,
  and accented letters (é) differ from plain ones (e).
- Languages written with spaces between words (English etc.) match from the start of a word:
  tent matches tents but not content. Languages without spaces (Chinese, Japanese, Thai...) match
  anywhere: 奶 matches 牛奶.
- Case-insensitive; full-width/half-width characters, curly/straight quotes (don’t / don't) and runs
  of whitespace are treated as the same.
"""

from __future__ import annotations

import re
import unicodedata

_TERM_RE = re.compile(r'"([^"]*)"|“([^”]*)”|([^\s,，、"“”]+)')
_QUOTES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "“": '"', "”": '"'})

# Scripts written without spaces between words: keywords in these may appear anywhere
_UNSPACED_RANGES = [
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x2E80, 0x2FFF),  # CJK radicals
    (0x3040, 0x31FF),  # Japanese kana, Bopomofo
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x3FFFF),  # CJK Extension B and later
]


def parse_terms(raw: str) -> list:
    terms = []
    for m in _TERM_RE.finditer(unicodedata.normalize("NFKC", raw or "")):
        term = " ".join((m.group(1) or m.group(2) or m.group(3) or "").split())
        if term and term not in terms:
            terms.append(term)
    return terms


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES).casefold()
    return " ".join(text.split())


def _is_word_char(ch: str) -> bool:
    """A letter or digit of a script that puts spaces between words (Latin, Cyrillic, Hangul, digits...)."""
    code = ord(ch)
    return ch.isalnum() and not any(lo <= code <= hi for lo, hi in _UNSPACED_RANGES)


def _contains(text: str, term: str) -> bool:
    if not _is_word_char(term[0]):
        return term in text
    start = text.find(term)
    while start != -1:
        # The match must start a word: the character before it can't be a letter of the same word
        if start == 0 or not _is_word_char(text[start - 1]):
            return True
        start = text.find(term, start + 1)
    return False


class Keywords:
    """A set of "must include" keywords; when empty, every text matches."""

    def __init__(self, raw: str = ""):
        self.terms = parse_terms(raw)
        self._normalized = [_normalize(t) for t in self.terms]

    def __bool__(self) -> bool:
        return bool(self.terms)

    def __str__(self) -> str:
        return ", ".join(f'"{t}"' if " " in t or "," in t else t for t in self.terms)

    def match(self, text: str) -> bool:
        if not self._normalized:
            return True
        normalized = _normalize(text)
        return all(_contains(normalized, term) for term in self._normalized)
