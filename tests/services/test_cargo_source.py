"""The ``cargo`` shipment source: this service creates the shipment through Cargo's API."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from teva_delivery.core.clock import ManualClock
from teva_delivery.domain.enums import Action, FulfillmentState, PrintMode
from teva_delivery.domain.errors import ProviderError, ServiceError
from teva_delivery.services.container import Services
from teva_delivery.services.views import FulfillmentView

from ..fakes import FlakyCarrier
from ..support import ADMIN, DANA, act, build, make_settings, pack_everything, settle, steps


@pytest.fixture
def cargo(tmp_path: Path, clock: ManualClock) -> Iterator[Services]:
    settings = make_settings(
        tmp_path,
        cargo_customer_code_express=1234,
        cargo_from_street="התעשייה",
        cargo_from_street_number="1",
        cargo_from_city="כרכור",
        cargo_from_phone="046000000",
    )
    services = build(settings, clock)
    services.context.providers.carrier = FlakyCarrier()
    services.overview.update_settings(ADMIN, {"shipment_source": "cargo"})
    services.sync.run()
    yield services
    services.context.db.dispose()


def carrier(services: Services) -> FlakyCarrier:
    found = services.context.providers.carrier
    assert isinstance(found, FlakyCarrier)
    return found


def packed(services: Services, category: str | None = None) -> FulfillmentView:
    if category:
        services.work.choose(DANA, category=category, batch=None)  # type: ignore[arg-type]
    view = services.work.next(DANA).fulfillment
    assert view is not None
    pack_everything(services, DANA, view)
    act(services, DANA, view.id, Action.COMPLETE_ORDER)
    return view


def test_the_service_creates_the_shipment_once(cargo: Services, clock: ManualClock) -> None:
    view = packed(cargo, "urgent")
    settle(cargo, clock)
    shipped = cargo.work.show(DANA, view.id)
    assert shipped.shipment is not None and shipped.shipment.state == "created"
    act(cargo, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    settle(cargo, clock)
    assert cargo.work.show(DANA, view.id).state is FulfillmentState.COMPLETED
    assert carrier(cargo).created == 1
    with pytest.raises(ServiceError):
        cargo.shipments.create(DANA, view.id)


def test_a_lost_answer_is_never_sent_again_by_itself(cargo: Services, clock: ManualClock) -> None:
    carrier(cargo).fail_with = ProviderError("cargo_unavailable", retryable=True, sent=True)
    view = packed(cargo, "urgent")
    settle(cargo, clock)
    uncertain = cargo.work.show(DANA, view.id)
    assert uncertain.shipment is not None and uncertain.shipment.state == "uncertain"
    assert steps(uncertain)["shipment"] == "uncertain"
    assert carrier(cargo).created == 0
    with pytest.raises(ServiceError) as error:
        cargo.shipments.create(DANA, view.id)
    assert error.value.code == "shipment_uncertain"

    # A person checked Cargo's site: it was not created. Now it may be sent again.
    cargo.shipments.resolve_not_created(ADMIN, view.id)
    settle(cargo, clock)
    assert carrier(cargo).created == 1
    assert cargo.work.show(DANA, view.id).shipment.state == "created"  # type: ignore[union-attr]


def test_a_refused_shipment_can_be_sent_again(cargo: Services, clock: ManualClock) -> None:
    carrier(cargo).fail_with = ProviderError("cargo_rejected", sent=False)
    view = packed(cargo, "urgent")
    settle(cargo, clock)
    failed = cargo.work.show(DANA, view.id)
    assert failed.shipment is not None and failed.shipment.error_code == "cargo_rejected"
    cargo.shipments.create(DANA, view.id)
    settle(cargo, clock)
    assert cargo.work.show(DANA, view.id).shipment.state == "created"  # type: ignore[union-attr]


def test_a_pickup_point_order_needs_its_number_linked(cargo: Services, clock: ManualClock) -> None:
    view = packed(cargo, "pickup_points")
    settle(cargo, clock)
    shown = cargo.work.show(DANA, view.id)
    assert shown.shipment is not None and shown.shipment.error_code == "pickup_point_unknown"
    cargo.shipments.link(DANA, view.id, "52999999")
    assert cargo.work.show(DANA, view.id).shipment.external_id == "52999999"  # type: ignore[union-attr]


def test_self_pickup_needs_no_shipment(cargo: Services, clock: ManualClock) -> None:
    cargo.work.choose(DANA, category=None, batch=None)
    queue = cargo.work.queue(DANA)
    pickup = next(item for item in queue.items if "20005" in item.order_ids)
    opened = cargo.work.open(DANA, pickup.id).fulfillment
    assert opened is not None
    pack_everything(cargo, DANA, opened)
    act(cargo, DANA, opened.id, Action.COMPLETE_ORDER)
    settle(cargo, clock)
    assert cargo.work.show(DANA, opened.id).state is FulfillmentState.COMPLETED
    with pytest.raises(ServiceError) as error:
        cargo.shipments.create(DANA, opened.id)
    assert error.value.code in ("shipment_not_required", "invalid_state")


def test_batch_creation_and_status_sync(cargo: Services, clock: ManualClock) -> None:
    queue = cargo.work.queue(DANA)
    ids = [item.id for item in queue.items]
    result = cargo.shipments.create_batch(DANA, ids)
    assert result["queued"] and result["skipped"]
    settle(cargo, clock)
    assert cargo.shipments.sync_statuses() == 0
    clock.advance(21 * 60)
    assert cargo.shipments.sync_statuses() >= 1


def test_a_cancelled_shipment_cannot_be_linked(cargo: Services) -> None:
    view = cargo.work.next(DANA).fulfillment
    assert view is not None
    with pytest.raises(ServiceError) as error:
        cargo.shipments.link(DANA, view.id, "52990000")
    assert error.value.code == "shipment_cancelled"


def test_konimbo_source_refuses_service_made_shipments(services: Services) -> None:
    services.sync.run()
    view = services.work.next(DANA).fulfillment
    assert view is not None
    with pytest.raises(ServiceError) as error:
        services.shipments.create(DANA, view.id)
    assert error.value.code == "shipments_by_konimbo"
