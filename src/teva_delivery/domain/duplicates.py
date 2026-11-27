"""Find orders that may belong together, and say why, for a person to decide.

Nothing here merges anything. Orders are grouped when they share a phone, an email or a
shipping address within a time window; each group is then classified:

- ``accidental_duplicate``: the same items and total, placed minutes apart, both payable.
- ``failed_payment_attempt``: one attempt's payment failed and another went through.
- ``already_fulfilled``: one of them was already packed; the open one is shown for context.
- ``consolidation_candidate``: different open orders to one address within a few days,
  which could travel in one box.
- ``intentional_multiple``: the same customer, days apart, different items.

A shared phone alone never makes orders duplicates; it only puts them in front of a person.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .enums import DuplicateKind
from .hebrew.text import normalize

ACCIDENTAL_WINDOW = timedelta(hours=3)
CONSOLIDATION_WINDOW = timedelta(days=4)
GROUP_WINDOW = timedelta(days=14)
_ADDRESS_NOISE = frozenset(
    normalize(word) for word in ("רחוב", "רח", "שדרות", "שד", "דירה", "קומה")
)


def phone_key(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    return digits if len(digits) >= 9 else None


def email_key(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    return value.strip().casefold()


def address_key(value: str | None) -> str | None:
    if not value:
        return None
    words = [word for word in normalize(value).split() if word not in _ADDRESS_NOISE]
    return " ".join(words) if len(words) >= 2 else None


@dataclass(frozen=True, slots=True)
class OrderFacts:
    order_id: str
    created_at: datetime
    phone: str | None
    email: str | None
    address: str | None
    # Sorted (sku or item id, quantity) pairs: equal for orders of the same items.
    items: tuple[tuple[str, int], ...]
    total: str | None
    payment_failed: bool = False
    # Packed, completed or otherwise out of the open queue.
    finished: bool = False


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    kind: DuplicateKind
    order_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    # For accidental duplicates and failed attempts: the orders that look like the extra copy.
    suspects: tuple[str, ...] = field(default=())

    @property
    def signature(self) -> str:
        return ",".join(sorted(self.order_ids))


def _keys(order: OrderFacts) -> list[tuple[str, str]]:
    keys = []
    if order.phone:
        keys.append(("phone", order.phone))
    if order.email:
        keys.append(("email", order.email))
    if order.address:
        keys.append(("address", order.address))
    return keys


def _components(orders: Sequence[OrderFacts]) -> list[list[int]]:
    parent = list(range(len(orders)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owners: dict[tuple[str, str], int] = {}
    for index, order in enumerate(orders):
        for key in _keys(order):
            # Orders arrive oldest first, so each one links to the latest order with its key.
            if key in owners:
                other = owners[key]
                if abs(orders[other].created_at - order.created_at) <= GROUP_WINDOW:
                    parent[find(index)] = find(other)
            owners[key] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(orders)):
        groups.setdefault(find(index), []).append(index)
    return [members for members in groups.values() if len(members) > 1]


def _shared(orders: Sequence[OrderFacts]) -> tuple[str, ...]:
    reasons = []
    for name, label in (
        ("phone", "אותו טלפון"),
        ("email", "אותו אימייל"),
        ("address", "אותה כתובת"),
    ):
        values = [getattr(order, name) for order in orders]
        if all(values) and len(set(values)) == 1:
            reasons.append(label)
    if not reasons:
        reasons.append("פרטי לקוח חופפים")
    return tuple(reasons)


def classify_group(orders: Sequence[OrderFacts]) -> DuplicateGroup | None:
    open_orders = [order for order in orders if not order.finished]
    if not open_orders:
        return None
    ids = tuple(order.order_id for order in sorted(orders, key=lambda order: order.created_at))
    reasons = list(_shared(orders))
    payable = [order for order in open_orders if not order.payment_failed]
    failed = [order for order in open_orders if order.payment_failed]

    if failed and any(
        order.items == failed[0].items for order in orders if not order.payment_failed
    ):
        reasons.append("ניסיון תשלום שנכשל ואחריו הזמנה זהה")
        return DuplicateGroup(
            DuplicateKind.FAILED_PAYMENT_ATTEMPT,
            ids,
            tuple(reasons),
            tuple(order.order_id for order in failed),
        )

    finished = [order for order in orders if order.finished]
    for order in open_orders:
        twin = next(
            (
                other
                for other in finished
                if other.items == order.items
                and abs(other.created_at - order.created_at) <= ACCIDENTAL_WINDOW
            ),
            None,
        )
        if twin is not None:
            reasons.append("הזמנה זהה כבר נארזה")
            return DuplicateGroup(
                DuplicateKind.ACCIDENTAL_DUPLICATE, ids, tuple(reasons), (order.order_id,)
            )

    by_items: dict[tuple[tuple[str, int], ...], list[OrderFacts]] = {}
    for order in payable:
        by_items.setdefault(order.items, []).append(order)
    for same in by_items.values():
        same.sort(key=lambda order: order.created_at)
        if (
            len(same) > 1
            and same[-1].created_at - same[0].created_at <= ACCIDENTAL_WINDOW
            and len({order.total for order in same}) == 1
        ):
            reasons.append("אותם מוצרים ואותו סכום בהפרש קצר")
            return DuplicateGroup(
                DuplicateKind.ACCIDENTAL_DUPLICATE,
                ids,
                tuple(reasons),
                tuple(order.order_id for order in same[1:]),
            )

    if finished and len(open_orders) >= 1:
        reasons.append("ללקוח יש הזמנה שכבר נארזה")
        if len(payable) < 2:
            return DuplicateGroup(DuplicateKind.ALREADY_FULFILLED, ids, tuple(reasons))

    if len(payable) >= 2:
        span = max(order.created_at for order in payable) - min(
            order.created_at for order in payable
        )
        same_address = len({order.address for order in payable}) == 1 and payable[0].address
        if span <= CONSOLIDATION_WINDOW and same_address:
            reasons.append("כמה הזמנות פתוחות לאותה כתובת")
            return DuplicateGroup(DuplicateKind.CONSOLIDATION_CANDIDATE, ids, tuple(reasons))
        reasons.append("הזמנות שונות בימים שונים")
        return DuplicateGroup(DuplicateKind.INTENTIONAL_MULTIPLE, ids, tuple(reasons))
    return None


def find_groups(orders: Iterable[OrderFacts]) -> list[DuplicateGroup]:
    ordered = sorted(orders, key=lambda order: (order.created_at, order.order_id))
    groups = []
    for members in _components(ordered):
        group = classify_group([ordered[index] for index in members])
        if group is not None:
            groups.append(group)
    groups.sort(key=lambda group: group.order_ids)
    return groups
