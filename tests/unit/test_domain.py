"""Notes, addresses, duplicate keys, packing rules and the shipping label check."""

from datetime import UTC, datetime, timedelta

import pytest

from teva_delivery.domain.addresses import parse_address
from teva_delivery.domain.duplicates import (
    OrderFacts,
    address_key,
    email_key,
    find_groups,
    phone_key,
)
from teva_delivery.domain.enums import DuplicateKind, FulfillmentState, LineStatus, NoteKind
from teva_delivery.domain.errors import ServiceError
from teva_delivery.domain.notes import split_instructions
from teva_delivery.domain.packing import (
    LineFacts,
    check_quantity,
    check_transition,
    line_status,
)
from teva_delivery.services.printing import label_matches

NOW = datetime(2026, 11, 26, 8, 0, tzinfo=UTC)


def test_a_customer_note_is_split_into_packing_and_delivery_instructions() -> None:
    parts = split_instructions("בבקשה לעטוף באריזת מתנה. להשאיר ליד הדלת", default=NoteKind.PACKING)
    assert [(part.text, part.kind) for part in parts] == [
        ("בבקשה לעטוף באריזת מתנה", NoteKind.PACKING),
        ("להשאיר ליד הדלת", NoteKind.DELIVERY),
    ]


def test_courtesy_is_information_and_empty_notes_are_nothing() -> None:
    assert [part.kind for part in split_instructions("תודה רבה!", default=NoteKind.PACKING)] == [
        NoteKind.INFO
    ]
    assert split_instructions("  ", default=NoteKind.PACKING) == []
    assert split_instructions(None, default=NoteKind.PACKING) == []


@pytest.mark.parametrize(
    ("text", "street", "city", "floor", "apartment"),
    [
        ("הרצל 12, תל אביב", "הרצל 12", "תל אביב", "", ""),
        ("הרצל 3 תל אביב", "הרצל 3", "תל אביב", "", ""),
        ("ז׳בוטינסקי 40 קומה 3 דירה 12, רמת גן", "ז׳בוטינסקי 40", "רמת גן", "3", "12"),
    ],
)
def test_addresses_are_split_for_cargo(
    text: str, street: str, city: str, floor: str, apartment: str
) -> None:
    address = parse_address(text)
    assert address is not None
    assert (address.street, address.city, address.floor, address.apartment) == (
        street,
        city,
        floor,
        apartment,
    )


def test_an_address_without_a_city_is_not_guessed() -> None:
    assert parse_address("תל אביב") is None
    assert parse_address(None) is None


def test_duplicate_keys_normalise_contact_details() -> None:
    assert phone_key("+972-50-123-4567") == "0501234567"
    assert phone_key("123") is None
    assert email_key(" Dana@Example.COM ") == "dana@example.com"
    assert address_key("רחוב הרצל 12, תל אביב") == address_key("הרצל 12 תל אביב")


def facts(order_id: str, minutes: float, **fields: object) -> OrderFacts:
    values: dict[str, object] = {
        "phone": "0501234567",
        "email": "dana@example.com",
        "address": "הרצל 12, תל אביב",
        "items": (("1001", 1),),
        "total": "59.00",
    }
    values.update(fields)
    return OrderFacts(order_id=order_id, created_at=NOW + timedelta(minutes=minutes), **values)  # type: ignore[arg-type]


def test_the_same_items_minutes_apart_are_an_accidental_duplicate() -> None:
    groups = find_groups([facts("1", 0), facts("2", 20)])
    assert len(groups) == 1
    assert groups[0].kind is DuplicateKind.ACCIDENTAL_DUPLICATE


def test_a_shared_phone_alone_is_only_for_review() -> None:
    groups = find_groups(
        [
            facts("1", 0, email=None, address="הרצל 12, תל אביב"),
            facts("2", 60 * 24 * 5, email=None, address="ויצמן 4, חיפה", items=(("1002", 2),)),
        ]
    )
    assert all(group.kind is not DuplicateKind.ACCIDENTAL_DUPLICATE for group in groups)


def test_line_status_and_quantities() -> None:
    assert line_status(2, 2, 0) is LineStatus.CONFIRMED
    assert line_status(2, 1, 0) is LineStatus.PARTIAL
    assert line_status(2, 0, 1) is LineStatus.MISSING
    line = LineFacts(id=1, title="סבון", required=2, confirmed=1, missing=0)
    check_quantity(line, 1)
    with pytest.raises(ServiceError) as error:
        check_quantity(line, 2)
    assert error.value.code == "quantity_exceeds_required"


def test_finished_orders_cannot_be_packed_again() -> None:
    with pytest.raises(ServiceError):
        check_transition(FulfillmentState.COMPLETED, FulfillmentState.PACKING)


@pytest.mark.parametrize(
    ("url", "number", "expected"),
    [
        ("https://api-v2.cargo.co.il/labels/2026-11-26/shipment_123456_abc.pdf", "123456", True),
        ("https://api-v2.cargo.co.il/labels/2026-11-26/shipment_1234567_abc.pdf", "123456", False),
        ("https://api-v2.cargo.co.il/labels/x/shipment_123456_a.pdf.exe", "123456", False),
        ("http://api-v2.cargo.co.il/labels/x/shipment_123456_a.pdf", "123456", False),
        ("https://evil.example/labels/x/shipment_123456_a.pdf", "123456", False),
        ("https://api-v2.cargo.co.il:8443/labels/x/shipment_123456_a.pdf", "123456", False),
        ("https://user@api-v2.cargo.co.il/labels/x/shipment_123456_a.pdf", "123456", False),
        ("https://api-v2.cargo.co.il/other/shipment_123456_a.pdf", "123456", False),
    ],
)
def test_a_label_is_only_printed_for_its_own_shipment(
    url: str, number: str, expected: bool
) -> None:
    assert label_matches(url, number) is expected
