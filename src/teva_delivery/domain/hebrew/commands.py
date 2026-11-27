"""Turn one spoken Hebrew sentence into validated-action requests, deterministically.

The parser never changes anything. It reads the sentence against the active order (its
products and packing notes) and returns commands that the packing service then validates.
A sentence that is not clearly addressed to the system (no command word, or a long sentence
that only happens to contain a product name) is ignored, so conversation at the packing table
does not confirm anything.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from ..enums import Action, PrintMode
from .matcher import MatchResult, Mention, Target, find_mentions, target_score
from .numbers import number_value
from .text import normalize, significant, tokens


def _words(*values: str) -> frozenset[str]:
    return frozenset(normalize(value) for value in values)


CONFIRM_VERBS = _words(
    "שמתי",
    "שמנו",
    "שמת",
    "הכנסתי",
    "הכנסנו",
    "ארזתי",
    "ארזנו",
    "הוספתי",
    "הוספנו",
    "לקחתי",
    "סימנתי",
    "צירפתי",
    "צרפתי",
    "צירפנו",
    "עטפתי",
    "עטפנו",
    "נכנס",
    "נכנסה",
    "נכנסו",
    "מאשר",
    "מאשרת",
    "אישור",
    "אשר",
    "תאשר",
    "תאשרי",
    "יש",
    "הנה",
    "בפנים",
    "ביצעתי",
    "עשיתי",
    "טיפלתי",
)
NOTE_HINTS = _words(
    "הערה",
    "ההערה",
    "הערות",
    "ההערות",
    "הוראה",
    "ההוראה",
    "בקשה",
    "הבקשה",
    "מתנה",
    "מתנת",
    "קבלה",
    "חשבונית",
    "ברכה",
    "עטיפה",
)
MISSING_WORDS = _words(
    "חסר",
    "חסרה",
    "חסרים",
    "חסרות",
    "אין",
    "נגמר",
    "נגמרה",
    "נגמרו",
    "אזל",
    "אזלה",
    "אזלו",
)
QUESTION_WORDS = _words("מה", "מהו", "מהי", "מהן", "מהם", "איזה", "אילו", "כמה", "האם")
REMAINING_WORDS = _words("חסר", "חסרים", "חסרות", "נשאר", "נשארו", "נשארה", "עוד", "נותר", "נותרו")
NOTES_WORDS = _words(
    "הערות", "ההערות", "הערה", "ההערה", "בהערה", "בהערות", "הוראות", "ההוראות", "בקשות", "הבקשות"
)
READ_VERBS = _words("תקריא", "תקריאי", "תקרא", "תקראי", "הקרא", "קרא", "תגיד", "תגידי", "תספר")
UNDO_WORDS = _words(
    "תבטל",
    "תבטלי",
    "בטל",
    "בטלי",
    "ביטול",
    "תמחק",
    "תמחקי",
    "מחק",
    "תוריד",
    "תורידי",
    "הורד",
    "טעות",
    "אופס",
)
LAST_WORDS = _words("האחרון", "אחרון", "האחרונה", "אחרונה")
PRINT_VERBS = _words(
    "תדפיס",
    "תדפיסי",
    "הדפס",
    "הדפיסי",
    "להדפיס",
    "הדפסה",
    "תוציא",
    "תוציאי",
    "מדפיס",
    "מדפיסה",
)
LABEL_WORDS = _words(
    "מדבקה",
    "המדבקה",
    "מדבקת",
    "מדבקות",
    "תווית",
    "התווית",
    "תוויות",
    "ברקוד",
    "הברקוד",
)
AGAIN_WORDS = _words("שוב", "מחדש", "חוזרת", "נוספת")
NEXT_WORDS = _words("הבאה", "הבא", "לבאה", "לבא", "הלאה")
PREVIOUS_WORDS = _words("הקודמת", "קודמת", "הקודם", "קודם", "לקודמת", "אחורה")
NAV_VERBS = _words(
    "עבור",
    "תעבור",
    "תעברי",
    "עברי",
    "תמשיך",
    "תמשיכי",
    "המשך",
    "תחזור",
    "תחזרי",
    "חזור",
    "חזרי",
    "תפתח",
    "תפתחי",
    "תביא",
    "תביאי",
)
ORDER_WORDS = _words("הזמנה", "ההזמנה", "להזמנה", "הזמנת")
COMPLETE_WORDS = _words(
    "סיימתי",
    "סיימנו",
    "סיים",
    "סיום",
    "גמרתי",
    "גמרנו",
    "מוכנה",
    "מוכן",
    "סגור",
    "תסגור",
    "תסגרי",
    "סגרתי",
)
PAUSE_WORDS = _words("תשהה", "תשהי", "השהה", "השהי", "השהיה", "הפסקה", "תקפיא", "תקפיאי")
SKIP_WORDS = _words("דלג", "תדלג", "תדלגי", "דלגי", "דילוג")
REASON_WORDS = _words("כי", "בגלל", "מפני", "סיבה")
HELP_WORDS = _words("עזרה", "הוראות שימוש")
CANCEL_WORDS = _words("לא", "ביטול", "עזוב", "עזבי", "שכח", "שכחי", "בטל")
ORDINALS = {
    normalize(word): index
    for index, words in enumerate(
        (
            ("ראשון", "הראשון", "ראשונה", "הראשונה"),
            ("שני", "השני", "שנייה", "השנייה", "שניה", "השניה"),
            ("שלישי", "השלישי", "שלישית", "השלישית"),
            ("רביעי", "הרביעי", "רביעית", "הרביעית"),
        )
    )
    for word in words
}
COMMAND_WORDS = (
    CONFIRM_VERBS
    | MISSING_WORDS
    | QUESTION_WORDS
    | READ_VERBS
    | UNDO_WORDS
    | LAST_WORDS
    | PRINT_VERBS
    | LABEL_WORDS
    | AGAIN_WORDS
    | NEXT_WORDS
    | PREVIOUS_WORDS
    | NAV_VERBS
    | ORDER_WORDS
    | COMPLETE_WORDS
    | PAUSE_WORDS
    | SKIP_WORDS
)
# A sentence with no command word is a confirmation only when it is this short and is mostly
# product words: "סרום שפע" counts, a remark that mentions a product does not.
BARE_MENTION_MAX_WORDS = 5
BARE_MENTION_MIN_COVERAGE = 0.6


@dataclass(frozen=True, slots=True)
class Clarification:
    """A question the assistant asked and is waiting on: which of these products?"""

    action: Action
    candidate_ids: tuple[int, ...]
    quantity: int | None = None
    everything: bool = False


@dataclass(frozen=True, slots=True)
class OrderContext:
    lines: tuple[Target, ...]
    notes: tuple[Target, ...]
    clarification: Clarification | None = None


@dataclass(frozen=True, slots=True)
class Command:
    action: Action
    # The one product or note the command is about, once resolved.
    target_id: int | None = None
    target_kind: Literal["line", "note"] | None = None
    quantity: int | None = None
    everything: bool = False
    # Ambiguous: the products it could be, for a question back.
    candidates: tuple[int, ...] = ()
    # Product words that matched nothing in the order.
    unknown: str | None = None
    reason: str | None = None
    print_mode: PrintMode | None = None
    # The words that produced this command, for the screen.
    words: str = ""


@dataclass(frozen=True, slots=True)
class ParseResult:
    commands: tuple[Command, ...] = ()
    # Why nothing was done: "empty", "unrelated" (not addressed to the system).
    ignored: str | None = None
    # The sentence answered the pending question by dropping it.
    clarification_cancelled: bool = False
    help: bool = False


def _has(words: Iterable[str], vocabulary: frozenset[str]) -> bool:
    return any(word in vocabulary for word in words)


def _after(words: Sequence[str], markers: frozenset[str]) -> str | None:
    for index, word in enumerate(words):
        if word in markers and index + 1 < len(words):
            return " ".join(words[index + 1 :])
    return None


def _content(words: Sequence[str]) -> list[str]:
    """The sentence without its command words, where product names are looked for."""
    return [word for word in words if word not in COMMAND_WORDS]


def _resolve(
    mention: Mention, action: Action, kind: Literal["line", "note"] | None = None
) -> Command:
    text = " ".join(mention.words)
    candidates = [target for target, _ in mention.candidates if kind is None or target.kind == kind]
    if len(candidates) == 1:
        target = candidates[0]
        return Command(
            action=action,
            target_id=target.id,
            target_kind=target.kind,
            quantity=mention.quantity,
            everything=mention.everything or (mention.quantity is None and mention.plural),
            words=text,
        )
    return Command(
        action=action,
        quantity=mention.quantity,
        everything=mention.everything or (mention.quantity is None and mention.plural),
        candidates=tuple(target.id for target in candidates),
        words=text,
    )


def _answer_clarification(words: list[str], context: OrderContext) -> ParseResult | None:
    pending = context.clarification
    if pending is None:
        return None
    if len(words) <= 3 and _has(words, CANCEL_WORDS) and not _has(words, CONFIRM_VERBS):
        return ParseResult(clarification_cancelled=True)
    choices = [
        target for target in (*context.lines, *context.notes) if target.id in pending.candidate_ids
    ]
    choices.sort(key=lambda target: pending.candidate_ids.index(target.id))
    if len(words) <= 3:
        for word in words:
            position = ORDINALS.get(word)
            if position is None and (value := number_value(word)) is not None and value <= 4:
                position = value - 1
            if position is not None and position < len(choices):
                chosen = choices[position]
                return ParseResult(
                    commands=(
                        Command(
                            action=pending.action,
                            target_id=chosen.id,
                            target_kind=chosen.kind,
                            quantity=pending.quantity,
                            everything=pending.everything,
                            words=" ".join(words),
                        ),
                    )
                )
    content = _content(words)
    if not content:
        return None
    scored = sorted(
        ((target, target_score(content, target)) for target in choices),
        key=lambda item: item[1],
        reverse=True,
    )
    if scored and scored[0][1] > 0 and (len(scored) == 1 or scored[0][1] - scored[1][1] >= 0.05):
        chosen = scored[0][0]
        return ParseResult(
            commands=(
                Command(
                    action=pending.action,
                    target_id=chosen.id,
                    target_kind=chosen.kind,
                    quantity=pending.quantity,
                    everything=pending.everything,
                    words=" ".join(words),
                ),
            )
        )
    return None


def parse(transcript: str, context: OrderContext) -> ParseResult:
    words = tokens(transcript)
    if not words:
        return ParseResult(ignored="empty")

    answered = _answer_clarification(words, context)
    if answered is not None:
        return answered

    if _has(words, HELP_WORDS) and len(words) <= 4:
        return ParseResult(help=True)

    question = _has(words, QUESTION_WORDS) or transcript.strip().endswith("?")
    if (question or _has(words, READ_VERBS)) and _has(words, NOTES_WORDS):
        return ParseResult(commands=(Command(Action.READ_ORDER_NOTES, words=transcript),))
    if words[0] == normalize("יש") and _has(words, NOTES_WORDS) and len(words) <= 3:
        return ParseResult(commands=(Command(Action.READ_ORDER_NOTES, words=transcript),))
    if question and _has(words, REMAINING_WORDS):
        return ParseResult(commands=(Command(Action.GET_REMAINING_PRODUCTS, words=transcript),))

    if (_has(words, PRINT_VERBS) and (_has(words, LABEL_WORDS) or len(words) <= 2)) or (
        len(words) == 1 and _has(words, LABEL_WORDS)
    ):
        mode = PrintMode.REPRINT if _has(words, AGAIN_WORDS) else PrintMode.MANUAL
        return ParseResult(
            commands=(Command(Action.PRINT_SHIPPING_LABEL, print_mode=mode, words=transcript),)
        )

    lines_and_notes = (*context.lines, *context.notes)
    if _has(words, UNDO_WORDS):
        found = find_mentions(_content(words), context.lines)
        if found.mentions and not _has(words, LAST_WORDS):
            return ParseResult(
                commands=tuple(
                    _resolve(mention, Action.UNDO_PRODUCT_CONFIRMATION, "line")
                    for mention in found.mentions
                )
            )
        return ParseResult(commands=(Command(Action.UNDO_PRODUCT_CONFIRMATION, words=transcript),))

    if _has(words, NEXT_WORDS) and (
        _has(words, NAV_VERBS) or _has(words, ORDER_WORDS) or len(words) <= 2
    ):
        return ParseResult(commands=(Command(Action.NEXT_ORDER, words=transcript),))
    if _has(words, PREVIOUS_WORDS) and (
        _has(words, NAV_VERBS) or _has(words, ORDER_WORDS) or len(words) <= 2
    ):
        return ParseResult(commands=(Command(Action.PREVIOUS_ORDER, words=transcript),))

    if _has(words, MISSING_WORDS) and not question:
        found = find_mentions(_content(words), context.lines)
        if found.mentions:
            return ParseResult(
                commands=tuple(
                    _resolve(mention, Action.REPORT_MISSING_INVENTORY, "line")
                    for mention in found.mentions
                )
            )
        unknown = " ".join(_content(words)) or None
        return ParseResult(
            commands=(Command(Action.REPORT_MISSING_INVENTORY, unknown=unknown, words=transcript),)
        )

    if _has(words, SKIP_WORDS):
        return ParseResult(
            commands=(
                Command(Action.SKIP_ORDER, reason=_after(words, REASON_WORDS), words=transcript),
            )
        )
    if _has(words, PAUSE_WORDS):
        return ParseResult(commands=(Command(Action.PAUSE_ORDER, words=transcript),))

    content = _content(words)
    found = find_mentions(content, lines_and_notes)
    verb = _has(words, CONFIRM_VERBS)
    completion = _has(words, COMPLETE_WORDS)
    if completion and not found.mentions:
        return ParseResult(commands=(Command(Action.COMPLETE_ORDER, words=transcript),))

    if found.mentions:
        if not verb and not completion and not _bare_mention(words, found):
            return ParseResult(ignored="unrelated")
        commands = []
        for mention in found.mentions:
            kinds = {target.kind for target, _ in mention.candidates}
            if kinds == {"note"} or (
                "note" in kinds and _has(words, NOTE_HINTS) and "line" not in kinds
            ):
                commands.append(_resolve(mention, Action.CONFIRM_ORDER_NOTE, "note"))
            elif kinds == {"line"}:
                commands.append(_resolve(mention, Action.CONFIRM_PRODUCT, "line"))
            else:
                # Both a product and a note fit. The best score decides; a tie asks.
                best_target, best_score = mention.candidates[0]
                rivals = [
                    target
                    for target, score in mention.candidates
                    if target.kind != best_target.kind and score >= best_score - 0.02
                ]
                if rivals:
                    commands.append(_resolve(mention, Action.CONFIRM_PRODUCT))
                else:
                    action = (
                        Action.CONFIRM_ORDER_NOTE
                        if best_target.kind == "note"
                        else Action.CONFIRM_PRODUCT
                    )
                    commands.append(_resolve(mention, action, best_target.kind))
        if found.dangling_quantity or (verb and found.unmatched and _looks_like_product(found)):
            commands.append(
                Command(Action.CONFIRM_PRODUCT, unknown=" ".join(found.unmatched), words=transcript)
            )
        return ParseResult(commands=tuple(commands))

    if (
        verb
        and _has(words, NOTES_WORDS)
        and not [word for word in significant(content) if word not in NOTES_WORDS]
    ):
        # "ביצעתי את ההערה": the only open packing note, if there is exactly one.
        return ParseResult(commands=(Command(Action.CONFIRM_ORDER_NOTE, words=transcript),))
    if verb and content:
        return ParseResult(
            commands=(Command(Action.CONFIRM_PRODUCT, unknown=" ".join(content), words=transcript),)
        )
    return ParseResult(ignored="unrelated")


def _bare_mention(words: Sequence[str], found: MatchResult) -> bool:
    return len(words) <= BARE_MENTION_MAX_WORDS and found.coverage >= BARE_MENTION_MIN_COVERAGE


def _looks_like_product(found: MatchResult) -> bool:
    """Leftover words after a verb are a product we did not find, unless they are chatter."""
    return (
        len(found.unmatched) >= 1
        and all(len(word) > 1 for word in found.unmatched)
        and (found.coverage < 0.5)
    )


@dataclass(frozen=True, slots=True)
class HelpText:
    examples: tuple[str, ...] = field(
        default=(
            "שמתי סרום שפע",
            "שמתי שתי משחות",
            "מה עוד חסר?",
            "מה ההערות?",
            "חסר לנו סבון",
            "תבטל את המוצר האחרון",
            "תדפיס מדבקה",
            "עבור להזמנה הבאה",
        )
    )
