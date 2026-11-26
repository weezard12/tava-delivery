"""Hebrew text normalisation for matching spoken words against product names.

Speech recognisers and product catalogues spell the same word differently: with or without
niqqud, final letters, geresh marks, prefixes (ו, ה, ב, ל, מ, ש, כ) and plural endings. Matching
works on normalised tokens and on the small set of forms each token could stand for.
"""

import re
import unicodedata
from functools import lru_cache

# Niqqud and cantillation marks.
_MARKS = re.compile("[֑-ׇ]")
_FINAL_LETTERS = str.maketrans({"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"})
# Geresh and gershayim inside abbreviations (מ"ל, ג׳ל) are dropped, other punctuation splits.
_ABBREVIATION_MARKS = re.compile("(?<=[א-ת])[\"'׳״’”](?=[א-ת])")
_SEPARATORS = re.compile(r"[^\w]+", re.UNICODE)
_HEBREW = re.compile("[א-ת]")

PREFIX_LETTERS = frozenset("והבלמשכ")
# Words that carry no product meaning in a packing sentence.
_STOPWORDS = {
    "את",
    "של",
    "עם",
    "גם",
    "עוד",
    "זה",
    "זאת",
    "אלה",
    "פה",
    "כאן",
    "לנו",
    "לי",
    "בבקשה",
    "טוב",
    "אוקיי",
    "אוקי",
    "יאללה",
    "נו",
    "אה",
    "אמ",
    "כן",
    "רגע",
    "the",
    "and",
}
# Size and unit words in product titles that nobody says while packing.
_UNIT_WORDS = {
    "מל",
    "מיל",
    "מיליליטר",
    "ליטר",
    "גרם",
    "גר",
    "קג",
    "קילו",
    "יחידות",
    "יחידה",
    "יח",
    "ml",
    "gr",
    "g",
    "kg",
    "oz",
    "מארז",
}


def normalize(text: str) -> str:
    """Lower case, no niqqud, no final letters, no punctuation, single spaces."""
    text = unicodedata.normalize("NFKC", text)
    text = _MARKS.sub("", text)
    text = _ABBREVIATION_MARKS.sub("", text)
    text = text.translate(_FINAL_LETTERS).casefold()
    text = _SEPARATORS.sub(" ", text).replace("_", " ")
    return " ".join(text.split())


def tokens(text: str) -> list[str]:
    return normalize(text).split()


def is_hebrew(token: str) -> bool:
    return bool(_HEBREW.search(token))


@lru_cache(maxsize=8192)
def prefix_forms(token: str) -> frozenset[str]:
    """The token and the words it becomes without its prefix letters ("והמשחה" -> "משחה")."""
    result = [token]
    current = token
    if is_hebrew(token):
        for _ in range(3):
            if len(current) > 3 and current[0] in PREFIX_LETTERS:
                current = current[1:]
                result.append(current)
            else:
                break
    return frozenset(result)


@lru_cache(maxsize=8192)
def forms(token: str) -> frozenset[str]:
    """The forms a spoken token could stand for: itself, without prefixes, singular.

    "והמשחות" -> {"והמשחות", "המשחות", "משחות", "משחה", "משח", ...}. Prefixes are only removed
    while at least two letters remain, so short words keep their meaning.
    """
    result = {token}
    if not is_hebrew(token):
        return frozenset(result)
    stems = [token]
    current = token
    # At most three prefix letters, as in "ושכש..." which never occurs in practice.
    for _ in range(3):
        if len(current) > 3 and current[0] in PREFIX_LETTERS:
            current = current[1:]
            stems.append(current)
        else:
            break
    for stem in stems:
        result.add(stem)
        result.update(_singular(stem))
    return frozenset(form for form in result if len(form) >= 2)


def _singular(word: str) -> set[str]:
    found: set[str] = set()
    if len(word) > 4 and word.endswith("ימ"):
        base = word[:-2]
        found.update({base, base + "ה"})
        # ידיימ -> ידי, ידיימ stays as well.
        if base.endswith("י"):
            found.add(base[:-1])
    if len(word) > 4 and word.endswith("ות"):
        base = word[:-2]
        found.update({base, base + "ה", base + "ת"})
    if len(word) > 3 and word.endswith("ה"):
        found.add(word[:-1])
    if len(word) > 3 and word.endswith("ת"):
        found.add(word[:-1])
    return found


def edit_distance(left: str, right: str, limit: int = 3) -> int:
    """Damerau-Levenshtein distance, giving up (returning ``limit + 1``) past ``limit``."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous_previous: list[int] = []
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if (
                i > 1
                and j > 1
                and left_char == right[j - 2]
                and left[i - 2] == right_char
                and previous_previous
            ):
                current[j] = min(current[j], previous_previous[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        previous_previous, previous = previous, current
    return previous[-1]


@lru_cache(maxsize=65536)
def token_similarity(spoken: str, written: str) -> float:
    """How well a spoken token matches a token of a product name, from 0 to 1.

    Equal forms score 1. Otherwise a small spelling difference between forms of four letters
    or more scores up to 0.85, so a recogniser's "סרומ" still finds "סירומ".
    """
    spoken_forms = forms(spoken)
    written_forms = forms(written)
    if spoken_forms & written_forms:
        return 1.0 if spoken == written or spoken_forms & {written} else 0.95
    best = 0.0
    for left in spoken_forms:
        for right in written_forms:
            longest = max(len(left), len(right))
            if min(len(left), len(right)) < 4:
                continue
            distance = edit_distance(left, right, limit=2)
            if distance <= 2:
                ratio = 1 - distance / longest
                if ratio >= 0.7:
                    best = max(best, round(0.85 * ratio + 0.1, 3))
    return min(best, 0.9)


def significant(words: list[str]) -> list[str]:
    """Drop stopwords, units and bare numbers from a product title's tokens."""
    return [
        word
        for word in words
        if word not in STOPWORDS and word not in UNIT_WORDS and not word.isdigit()
    ]


STOPWORDS = frozenset(normalize(word) for word in _STOPWORDS)
UNIT_WORDS = frozenset(normalize(word) for word in _UNIT_WORDS)
