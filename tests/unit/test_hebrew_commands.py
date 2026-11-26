"""The deterministic Hebrew parser: the sentences of the specification, and the hard cases."""

import pytest

from teva_delivery.domain.enums import Action, PrintMode
from teva_delivery.domain.hebrew.commands import Clarification, Command, OrderContext, parse
from teva_delivery.domain.hebrew.matcher import Target
from teva_delivery.domain.hebrew.text import normalize, tokens

LINES = (
    Target.build(1, "line", "סרום שפע לשיער 50 מ״ל", ["סרום שפע לשיער 50 מ״ל"]),
    Target.build(2, "line", "סבון זית טבעי", ["סבון זית טבעי"]),
    Target.build(3, "line", "משחת שיניים מנטה", ["משחת שיניים מנטה"]),
    Target.build(4, "line", "סרום ויטמין C כתום", ["סרום ויטמין C כתום", "הסרום הכתום"]),
)
NOTES = (Target.for_note(10, "הוראת אריזה", "לעטוף באריזת מתנה"),)
ORDER = OrderContext(LINES, NOTES)


def only(transcript: str, context: OrderContext = ORDER) -> Command:
    result = parse(transcript, context)
    assert len(result.commands) == 1, result
    return result.commands[0]


@pytest.mark.parametrize(
    ("transcript", "action"),
    [
        ("מה עוד חסר?", Action.GET_REMAINING_PRODUCTS),
        ("מה ההערות?", Action.READ_ORDER_NOTES),
        ("תבטל את המוצר האחרון", Action.UNDO_PRODUCT_CONFIRMATION),
        ("עבור להזמנה הבאה", Action.NEXT_ORDER),
        ("תחזור להזמנה הקודמת", Action.PREVIOUS_ORDER),
        ("תשהה את ההזמנה", Action.PAUSE_ORDER),
        ("סיימתי את ההזמנה", Action.COMPLETE_ORDER),
    ],
)
def test_commands_without_a_product(transcript: str, action: Action) -> None:
    command = only(transcript)
    assert command.action is action
    assert command.target_id is None


def test_confirms_a_product_by_its_first_words() -> None:
    command = only("שמתי סרום שפע")
    assert (command.action, command.target_id, command.quantity) == (
        Action.CONFIRM_PRODUCT,
        1,
        None,
    )


def test_reads_spoken_quantities_and_plurals() -> None:
    two = only("שמתי שתי משחות")
    assert (two.target_id, two.quantity) == (3, 2)
    three = only("שמתי שלושה סבונים")
    assert (three.target_id, three.quantity) == (2, 3)
    everything = only("שמתי את כל הסבונים")
    assert everything.target_id == 2 and everything.everything


def test_two_products_in_one_sentence() -> None:
    result = parse("שמתי סבון ומשחת שיניים", ORDER)
    assert [(c.action, c.target_id) for c in result.commands] == [
        (Action.CONFIRM_PRODUCT, 2),
        (Action.CONFIRM_PRODUCT, 3),
    ]


def test_an_alias_names_the_product() -> None:
    assert only("שמתי את הסרום הכתום").target_id == 4


def test_an_ambiguous_name_offers_the_candidates() -> None:
    command = only("שמתי סרום")
    assert command.target_id is None
    assert set(command.candidates) == {1, 4}


def test_a_product_not_in_the_order_is_unknown() -> None:
    command = only("שמתי קרם פנים")
    assert command.action is Action.CONFIRM_PRODUCT
    assert command.target_id is None and command.unknown


def test_missing_inventory_names_the_product() -> None:
    command = only("חסר לנו סבון")
    assert (command.action, command.target_id) == (Action.REPORT_MISSING_INVENTORY, 2)


def test_a_packing_note_is_confirmed_by_repeating_it() -> None:
    command = only("עטפתי באריזת מתנה")
    assert (command.action, command.target_id) == (Action.CONFIRM_ORDER_NOTE, 10)


@pytest.mark.parametrize("transcript", ["עשיתי את ההערה", "ביצעתי את ההערה", "טיפלתי בהערה"])
def test_the_note_without_its_words_means_the_only_open_one(transcript: str) -> None:
    command = only(transcript)
    assert (command.action, command.target_id, command.unknown) == (
        Action.CONFIRM_ORDER_NOTE,
        None,
        None,
    )


def test_printing_and_reprinting() -> None:
    assert only("תדפיס מדבקה").print_mode is PrintMode.MANUAL
    assert only("תדפיס שוב את המדבקה").print_mode is PrintMode.REPRINT


def test_skip_keeps_the_reason() -> None:
    command = only("דלג על ההזמנה כי הלקוח ביטל")
    assert command.action is Action.SKIP_ORDER
    assert command.reason == "הלקוח ביטל"


def test_chatter_and_silence_are_ignored() -> None:
    assert parse("מה נשמע אחי, איך היה אתמול", ORDER).ignored == "unrelated"
    assert parse("", ORDER).ignored == "empty"
    assert parse("עזרה", ORDER).help


def test_answers_to_a_question_about_which_product() -> None:
    pending = OrderContext(LINES, NOTES, Clarification(Action.CONFIRM_PRODUCT, (1, 4)))
    assert only("השני", pending).target_id == 4
    assert only("הכתום", pending).target_id == 4
    cancelled = parse("עזוב", pending)
    assert cancelled.clarification_cancelled and not cancelled.commands


def test_normalization_ignores_final_letters_and_niqqud() -> None:
    assert normalize("שָׁמְתִּי סבון") == normalize("שמתי סבונ")
    assert tokens("מה עוד חסר?") == ["מה", "עוד", "חסר"]
