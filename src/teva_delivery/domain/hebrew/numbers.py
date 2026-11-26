"""Spoken Hebrew quantities: "שתי משחות", "שלושה קרמים", "זוג", "2"."""

from .text import normalize

_UNITS = {
    1: ("אחד", "אחת"),
    2: ("שתיימ", "שתימ", "שתי", "שניימ", "שנימ", "שני", "זוג"),
    3: ("שלוש", "שלושה", "שלושת"),
    4: ("ארבע", "ארבעה", "ארבעת"),
    5: ("חמש", "חמישה", "חמשה", "חמשת"),
    6: ("שש", "שישה", "ששה", "ששת"),
    7: ("שבע", "שבעה", "שבעת"),
    8: ("שמונה", "שמונת"),
    9: ("תשע", "תשעה", "תשעת"),
    10: ("עשר", "עשרה", "עשרת"),
}
WORD_VALUES: dict[str, int] = {
    normalize(word): value for value, words in _UNITS.items() for word in words
}
# "כל ה..." and "את כולם" ask for everything that is still missing.
ALL_WORDS = frozenset({"כל", "כולמ", "כולנ", "הכל", "הכול", "כולה", "כולו"})
MAX_SPOKEN_QUANTITY = 99


def number_value(token: str) -> int | None:
    """The quantity a single token says, allowing a leading "ו" ("ושתי")."""
    for candidate in (token, token[1:] if token.startswith("ו") and len(token) > 2 else None):
        if candidate is None:
            continue
        if candidate.isdigit():
            value = int(candidate)
            return value if 0 < value <= MAX_SPOKEN_QUANTITY else None
        if candidate in WORD_VALUES:
            return WORD_VALUES[candidate]
    return None


def read_quantity(words: list[str], start: int) -> tuple[int | None, int]:
    """Read a quantity starting at ``words[start]``.

    Returns the value and how many tokens it used. Handles "אחד עשר" style teens and
    "עשרים" is not needed at a packing table, so it stops at 19.
    """
    first = number_value(words[start]) if start < len(words) else None
    if first is None:
        return None, 0
    if first < 10 and start + 1 < len(words) and words[start + 1] in ("עשר", "עשרה"):
        return first + 10, 2
    return first, 1


def is_all_word(token: str) -> bool:
    return token in ALL_WORDS or (token.startswith("ו") and token[1:] in ALL_WORDS)
