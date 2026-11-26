"""Split order notes into instructions and tell packing instructions from delivery ones.

A packing instruction ("לצרף מתנת יום הולדת", "לצרף קבלה") must be confirmed before the order
can be completed. A delivery instruction ("להשאיר ליד הדלת") goes to Cargo with the shipment.
Anything unclear in a customer's note is treated as a packing instruction, so it is read
and confirmed rather than missed; the employee can change the kind on the screen.
"""

import re
from dataclasses import dataclass

from .enums import NoteKind
from .hebrew.text import normalize, tokens

_SENTENCE_BREAK = re.compile(r"(?:\r?\n)+|(?<=[.!?;])\s+|\s+-\s+|\s*\|\s*")
MAX_NOTE_LENGTH = 1000

_DELIVERY = tuple(
    normalize(word)
    for word in (
        "שליח",
        "השליח",
        "לשליח",
        "משלוח",
        "להשאיר",
        "תשאירו",
        "תשאיר",
        "להניח",
        "ליד הדלת",
        "דלת",
        "שומר",
        "השומר",
        "קומה",
        "דירה",
        "כניסה",
        "אינטרקום",
        "קוד",
        "להתקשר",
        "תתקשרו",
        "תתקשר",
        "צלצול",
        "לצלצל",
        "לפני הגעה",
        "לפני שמגיעים",
        "לא בבית",
        "מרפסת",
        "לובי",
        "תיבת דואר",
        "שכן",
        "שכנה",
        "השכנים",
        "חניה",
        "שער",
        "בשעות",
        "אחרי השעה",
        "עד השעה",
        "ליד הבית",
    )
)
_PACKING = tuple(
    normalize(word)
    for word in (
        "מתנה",
        "מתנת",
        "כמתנה",
        "לעטוף",
        "עטיפה",
        "עטיפת",
        "אריזת מתנה",
        "קבלה",
        "חשבונית",
        "ברכה",
        "כרטיס",
        "פתק",
        "לצרף",
        "תצרפו",
        "תצרף",
        "להוסיף",
        "תוסיפו",
        "תוסיף",
        "דוגמית",
        "דוגמיות",
        "בלי מחיר",
        "ללא מחיר",
        "להוריד מחיר",
        "להוריד את המחיר",
        "שקית",
        "לארוז",
        "אריזה",
        "יום הולדת",
    )
)
_COURTESY = frozenset(
    normalize(word)
    for word in (
        "תודה",
        "תודה רבה",
        "בתודה",
        "יום טוב",
        "שבוע טוב",
        "שבת שלום",
        "בבקשה",
        "רב תודות",
    )
)


@dataclass(frozen=True, slots=True)
class Instruction:
    text: str
    kind: NoteKind


def _contains(text: str, phrases: tuple[str, ...]) -> bool:
    padded = f" {text} "
    return any(f" {phrase} " in padded or phrase in text.split() for phrase in phrases)


def classify(text: str, *, default: NoteKind) -> NoteKind:
    normalized = normalize(text)
    words = tokens(text)
    if not words or normalized in _COURTESY:
        return NoteKind.INFO
    packing = _contains(normalized, _PACKING)
    delivery = _contains(normalized, _DELIVERY)
    if packing:
        # "לעטוף כמתנה ולהשאיר ליד הדלת" is split earlier; a single sentence with both is
        # something to do in the box first.
        return NoteKind.PACKING
    if delivery:
        return NoteKind.DELIVERY
    return default


def split_instructions(note: str | None, *, default: NoteKind) -> list[Instruction]:
    """One customer note or office comment, as separate instructions in their original order."""
    if not note or not note.strip():
        return []
    text = note.strip()[:MAX_NOTE_LENGTH]
    parts = [part.strip(" ,.;") for part in _SENTENCE_BREAK.split(text)]
    pieces: list[str] = []
    for part in parts:
        if not part:
            continue
        # "לעטוף כמתנה ולהשאיר ליד הדלת": split where a delivery clause starts with "ו".
        pieces.extend(_split_clauses(part))
    instructions = [Instruction(piece, classify(piece, default=default)) for piece in pieces]
    if all(item.kind is NoteKind.INFO for item in instructions):
        return [Instruction(text, NoteKind.INFO)] if instructions else []
    return [item for item in instructions if item.kind is not NoteKind.INFO]


def _split_clauses(sentence: str) -> list[str]:
    words = sentence.split()
    for index in range(1, len(words)):
        word = words[index]
        if not word.startswith("ו") or len(word) < 3:
            continue
        before = " ".join(words[:index])
        after = " ".join([word[1:], *words[index + 1 :]])
        kinds = {
            classify(before, default=NoteKind.INFO),
            classify(after, default=NoteKind.INFO),
        }
        if kinds == {NoteKind.PACKING, NoteKind.DELIVERY}:
            return [before, " ".join(words[index:])]
    return [sentence]
