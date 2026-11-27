"""One order from the queue to the courier, as demo mode runs it, and what must never happen."""

import pytest

from teva_delivery.core.clock import ManualClock
from teva_delivery.domain.enums import Action, Category, FulfillmentState, PrintMode
from teva_delivery.domain.errors import ServiceError
from teva_delivery.providers.demo import DemoPrinter, DemoStore
from teva_delivery.services.completion import OWN_MARKER
from teva_delivery.services.container import Services

from ..support import DANA, YOSSI, act, pack_everything, settle, steps


@pytest.fixture
def ready(services: Services) -> Services:
    services.sync.run()
    return services


def store(services: Services) -> DemoStore:
    found = services.context.providers.store
    assert isinstance(found, DemoStore)
    return found


def printer(services: Services) -> DemoPrinter:
    found = services.context.providers.printer
    assert isinstance(found, DemoPrinter)
    return found


def test_the_import_sorts_orders_into_categories(ready: Services) -> None:
    queue = ready.work.queue(DANA)
    counts = {count.category: count.ready for count in queue.counts}
    assert counts == {
        Category.CONSOLIDATION: 1,
        Category.URGENT: 1,
        Category.PICKUP_POINTS: 1,
        Category.REGULAR: 3,
    }
    # The two identical orders placed half an hour apart wait for a person.
    assert queue.review == 2


def test_an_order_goes_from_packing_to_pickup(ready: Services, clock: ManualClock) -> None:
    view = ready.work.next(DANA).fulfillment
    assert view is not None and view.category is Category.CONSOLIDATION
    order_id = view.orders[0].id
    pack_everything(ready, DANA, view)
    done = act(ready, DANA, view.id, Action.COMPLETE_ORDER)
    assert done.ok and done.fulfillment is not None
    assert done.fulfillment.state is FulfillmentState.PACKED

    # The service sets the status that sends the order to Cargo, then finds the number
    # Konimbo's integration wrote into the order history.
    settle(ready, clock)
    packed = ready.work.show(DANA, view.id)
    assert packed.shipment is not None and packed.shipment.external_id
    assert steps(packed)["label"] == "pending"

    printed = act(ready, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    assert printed.ok
    assert printer(ready).printed[0].endswith(f"shipment_{packed.shipment.external_id}_demo.pdf")

    settle(ready, clock)
    finished = ready.work.show(DANA, view.id)
    assert finished.state is FulfillmentState.COMPLETED
    history = store(ready).get_order(order_id)
    assert history.status_title == ready.context.config().status_awaiting_pickup
    ours = [
        status for status in history.statuses if status.comment and OWN_MARKER in status.comment
    ]
    assert [status.title for status in ours] == ["הועבר ל-Cargo", "ממתין לאיסוף"]


def test_a_finished_order_is_never_shipped_or_printed_again(
    ready: Services, clock: ManualClock
) -> None:
    view = ready.work.next(DANA).fulfillment
    assert view is not None
    pack_everything(ready, DANA, view)
    act(ready, DANA, view.id, Action.COMPLETE_ORDER)
    settle(ready, clock)
    act(ready, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    settle(ready, clock)

    with pytest.raises(ServiceError) as again:
        act(ready, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    assert again.value.code == "already_printed"
    with pytest.raises(ServiceError) as complete:
        act(ready, DANA, view.id, Action.COMPLETE_ORDER)
    assert complete.value.code in ("order_finished", "already_packed")
    assert len(printer(ready).printed) == 1
    # An explicit reprint is allowed and recorded.
    act(ready, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.REPRINT)
    assert len(printer(ready).printed) == 2


def test_two_employees_never_pack_the_same_order(ready: Services) -> None:
    mine = ready.work.next(DANA).fulfillment
    theirs = ready.work.next(YOSSI).fulfillment
    assert mine is not None and theirs is not None and mine.id != theirs.id
    with pytest.raises(ServiceError) as error:
        ready.work.open(YOSSI, mine.id)
    assert error.value.code == "fulfillment_locked"
    line = mine.lines[0]
    with pytest.raises(ServiceError):
        act(ready, YOSSI, mine.id, Action.CONFIRM_PRODUCT, line_id=line.id)


def test_an_abandoned_order_is_paused_and_can_be_taken(ready: Services, clock: ManualClock) -> None:
    mine = ready.work.next(DANA).fulfillment
    assert mine is not None
    act(ready, DANA, mine.id, Action.CONFIRM_PRODUCT, line_id=mine.lines[0].id)
    clock.advance(34 * 60)
    assert ready.work.pause_abandoned() == 1
    taken = ready.work.open(YOSSI, mine.id).fulfillment
    assert taken is not None
    assert taken.progress.units_confirmed == 1


def test_the_same_request_twice_confirms_once(ready: Services) -> None:
    view = ready.work.next(DANA).fulfillment
    assert view is not None
    line = next(line for line in view.lines if line.required >= 2)
    for _ in range(2):
        result = act(
            ready,
            DANA,
            view.id,
            Action.CONFIRM_PRODUCT,
            line_id=line.id,
            idempotency_key="tap-000001",
        )
    assert result.fulfillment is not None
    confirmed = next(row for row in result.fulfillment.lines if row.id == line.id)
    assert confirmed.confirmed == 1
    with pytest.raises(ServiceError) as error:
        act(
            ready,
            DANA,
            view.id,
            Action.CONFIRM_PRODUCT,
            line_id=line.id,
            quantity=2,
            idempotency_key="tap-000001",
        )
    assert error.value.code == "idempotency_conflict"


def test_quantities_and_undo(ready: Services) -> None:
    view = ready.work.next(DANA).fulfillment
    assert view is not None
    line = next(line for line in view.lines if line.required >= 2)
    with pytest.raises(ServiceError) as error:
        act(
            ready,
            DANA,
            view.id,
            Action.CONFIRM_PRODUCT,
            line_id=line.id,
            quantity=line.required + 1,
        )
    assert error.value.code == "quantity_exceeds_required"
    act(ready, DANA, view.id, Action.CONFIRM_PRODUCT, line_id=line.id, quantity=2)
    undone = act(ready, DANA, view.id, Action.UNDO_PRODUCT_CONFIRMATION)
    assert undone.fulfillment is not None
    assert next(row for row in undone.fulfillment.lines if row.id == line.id).confirmed == 0
    with pytest.raises(ServiceError) as nothing:
        act(ready, DANA, view.id, Action.UNDO_PRODUCT_CONFIRMATION)
    assert nothing.value.code == "nothing_to_undo"


def test_an_incomplete_order_cannot_be_completed(ready: Services) -> None:
    view = ready.work.next(DANA).fulfillment
    assert view is not None
    with pytest.raises(ServiceError) as error:
        act(ready, DANA, view.id, Action.COMPLETE_ORDER)
    assert error.value.code == "incomplete_order"


def test_missing_stock_moves_the_order_out_and_back(ready: Services, clock: ManualClock) -> None:
    view = ready.work.next(DANA).fulfillment
    assert view is not None
    order_id = view.orders[0].id
    line = view.lines[0]
    act(ready, DANA, view.id, Action.REPORT_MISSING_INVENTORY, line_id=line.id, reason="נגמר במלאי")
    ready.work.next(DANA)
    settle(ready, clock, rounds=2)
    waiting = ready.work.show(DANA, view.id)
    assert waiting.state is FulfillmentState.AWAITING_INVENTORY
    konimbo = store(ready).get_order(order_id)
    assert konimbo.status_title == "ממתין למלאי"
    assert any("חסר" in (status.comment or "") for status in konimbo.statuses)

    ready.work.resolve_inventory(DANA, view.id)
    settle(ready, clock, rounds=2)
    back = ready.work.show(DANA, view.id)
    assert back.state is FulfillmentState.READY
    assert store(ready).get_order(order_id).status_title == "איחוד הזמנות"


def test_previous_returns_to_the_last_order(ready: Services) -> None:
    first = ready.work.next(DANA).fulfillment
    assert first is not None
    act(ready, DANA, first.id, Action.CONFIRM_PRODUCT, line_id=first.lines[0].id)
    second = ready.work.next(DANA).fulfillment
    assert second is not None and second.id != first.id
    back = ready.work.previous(DANA).fulfillment
    assert back is not None and back.id == first.id
    assert back.progress.units_confirmed == 1


def test_skipping_needs_a_reason(ready: Services) -> None:
    view = ready.work.next(DANA).fulfillment
    assert view is not None
    with pytest.raises(ServiceError) as error:
        ready.work.skip(DANA, view.id, " ")
    assert error.value.code == "skip_reason_required"
    after = ready.work.skip(DANA, view.id, "הלקוח ביקש לחכות")
    assert after.fulfillment is None or after.fulfillment.id != view.id
