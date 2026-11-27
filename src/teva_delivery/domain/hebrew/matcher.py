"""Find the products (or packing notes) a sentence mentions, with the quantity said for each.

Only the active order's own products are candidates, so a short list is searched and a
mention is either clearly one product, ambiguous between a few, or nothing.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .numbers import is_all_word, number_value, read_quantity
from .text import STOPWORDS, forms, normalize, prefix_forms, significant, token_similarity, tokens

# A span must score at least this to count as a mention.
MATCH_THRESHOLD = 0.62
# How alike a spoken word and a written one must be to count as the same word.
MATCH_WORD = 0.7
# Candidates within this distance of the best score make the mention ambiguous.
AMBIGUITY_MARGIN = 0.08
MAX_SPAN = 6
FILLERS = (
    frozenset(
        normalize(word) for word in ("ו", "גם", "וגם", "עוד", "ועוד", "את", "ואת", "של", "עם")
    )
    | STOPWORDS
)


@dataclass(frozen=True, slots=True)
class Target:
    """Something a sentence can refer to: a product row or a packing note."""

    id: int
    kind: Literal["line", "note"]
    label: str
    phrases: tuple[tuple[str, ...], ...]

    @classmethod
    def build(
        cls, id: int, kind: Literal["line", "note"], label: str, names: Iterable[str]
    ) -> Target:
        phrases: list[tuple[str, ...]] = []
        for name in names:
            words = significant(tokens(name))
            if words and tuple(words) not in phrases:
                phrases.append(tuple(words))
            # People name products by their first words: "סרום שפע" for a long title.
            if len(words) > 2 and tuple(words[:2]) not in phrases:
                phrases.append(tuple(words[:2]))
        return cls(id=id, kind=kind, label=label, phrases=tuple(phrases))

    @classmethod
    def for_note(cls, id: int, label: str, text: str) -> Target:
        """A note is a sentence; people repeat a piece of it ("צירפתי מתנת יום הולדת")."""
        words = significant(tokens(text))
        phrases: list[tuple[str, ...]] = []
        if words:
            phrases.append(tuple(words))
        for size in (2, 3, 4):
            for start in range(len(words) - size + 1):
                window = tuple(words[start : start + size])
                if window not in phrases:
                    phrases.append(window)
        return cls(id=id, kind="note", label=label, phrases=tuple(phrases))


@dataclass(frozen=True, slots=True)
class Mention:
    start: int
    end: int
    words: tuple[str, ...]
    quantity: int | None = None
    everything: bool = False
    plural: bool = False
    candidates: tuple[tuple[Target, float], ...] = ()

    @property
    def best(self) -> Target | None:
        return self.candidates[0][0] if len(self.candidates) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


@dataclass(slots=True)
class MatchResult:
    mentions: list[Mention] = field(default_factory=list)
    # Words that are neither fillers, quantities nor part of a mention.
    unmatched: list[str] = field(default_factory=list)
    # A quantity or "all" that was said for something that matched nothing.
    dangling_quantity: bool = False

    @property
    def coverage(self) -> float:
        used = sum(mention.end - mention.start for mention in self.mentions)
        total = used + len(self.unmatched)
        return used / total if total else 0.0


def phrase_score(span: Sequence[str], phrase: Sequence[str]) -> float:
    """Recall over the phrase (did the speaker say its words?) and precision over the span."""
    if not span or not phrase:
        return 0.0
    used: set[int] = set()
    recall = 0.0
    for written in phrase:
        best, best_index = 0.0, -1
        for index, spoken in enumerate(span):
            if index in used:
                continue
            score = token_similarity(spoken, written)
            if score > best:
                best, best_index = score, index
        if best_index >= 0 and best >= MATCH_WORD:
            used.add(best_index)
            recall += best
    if not used:
        return 0.0
    precision = len(used) / len(span)
    return round(0.65 * (recall / len(phrase)) + 0.35 * precision, 4)


def target_score(span: Sequence[str], target: Target) -> float:
    return max((phrase_score(span, phrase) for phrase in target.phrases), default=0.0)


def _is_plural(span: Sequence[str], target: Target) -> bool:
    """The speaker used a plural the product name does not have: "משחות" for "משחה"."""
    written = {word for phrase in target.phrases for word in phrase}
    for spoken in span:
        stems = prefix_forms(spoken)
        if stems & written:
            continue
        if any(stem.endswith(("ימ", "ות")) for stem in stems) and forms(spoken) & written:
            return True
    return False


def _candidates(span: Sequence[str], targets: Sequence[Target]) -> list[tuple[Target, float]]:
    scored = [(target, target_score(span, target)) for target in targets]
    scored = [item for item in scored if item[1] >= MATCH_THRESHOLD]
    scored.sort(key=lambda item: item[1], reverse=True)
    if not scored:
        return []
    top = scored[0][1]
    return [item for item in scored if item[1] >= top - AMBIGUITY_MARGIN]


def _starts(span: Sequence[str], target: Target) -> bool:
    return any(
        token_similarity(span[0], written) >= MATCH_WORD
        for phrase in target.phrases
        for written in phrase
    )


def find_mentions(words: Sequence[str], targets: Sequence[Target]) -> MatchResult:
    """Read a sentence left to right: quantities, then the product they belong to."""
    result = MatchResult()
    known = {word for target in targets for phrase in target.phrases for word in phrase}
    quantity: int | None = None
    everything = False
    index = 0
    while index < len(words):
        word = words[index]
        if word in FILLERS and word not in known:
            index += 1
            continue
        value, used = read_quantity(list(words), index)
        if value is not None and word not in known:
            quantity, index = value, index + used
            continue
        if is_all_word(word):
            everything, index = True, index + 1
            continue

        best: tuple[int, list[tuple[Target, float]]] | None = None
        best_score = 0.0
        for length in range(min(MAX_SPAN, len(words) - index), 0, -1):
            span = words[index : index + length]
            # A span never runs over a quantity that belongs to the next product.
            if any(number_value(token) is not None and token not in known for token in span[1:]):
                continue
            found = _candidates(span, targets)
            # A mention starts with one of its own words, so "סבון ומשחת שיניים" is two
            # products and not toothpaste with a stray word in front.
            if found and not _starts(span, found[0][0]):
                continue
            if found and found[0][1] > best_score + 0.02:
                best, best_score = (length, found), found[0][1]
        if best is None:
            result.unmatched.append(word)
            if quantity is not None or everything:
                # The quantity belonged to this word, which is not in the order.
                result.dangling_quantity = True
                quantity, everything = None, False
            index += 1
            continue

        length, found = best
        span = tuple(words[index : index + length])
        result.mentions.append(
            Mention(
                start=index,
                end=index + length,
                words=span,
                quantity=quantity,
                everything=everything,
                plural=any(_is_plural(span, target) for target, _ in found),
                candidates=tuple(found),
            )
        )
        quantity, everything = None, False
        index += length
    if quantity is not None or everything:
        result.dangling_quantity = True
    return result
