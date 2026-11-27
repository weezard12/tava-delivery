"""Konimbo writes that go wrong, finding the Cargo number, labels, and test mode."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from teva_delivery.core.clock import ManualClock
from teva_delivery.core.config import Settings
from teva_delivery.domain.enums import Action, FulfillmentState, JobState, PrintMode
from teva_delivery.domain.errors import ServiceError
from teva_delivery.persistence.database import Database
from teva_delivery.persistence.models import Job
from teva_delivery.services.completion import OWN_MARKER
from teva_delivery.services.container import Services, build_services
from teva_delivery.services.context import Context, Providers
from teva_delivery.services.views import FulfillmentView

from ..fakes import FlakyStore, UncertainPrinter, WrongLabelCarrier
from ..support import DANA, act, build, make_settings, pack_everything, settle, steps


@pytest.fixture
def flaky(tmp_path: Path, clock: ManualClock) -> Iterator[Services]:
    services = build(make_settings(tmp_path), clock)
    services.context.providers.store = FlakyStore(clock, services.context.config)
    services.context.providers.printer = UncertainPrinter()
    services.sync.run()
    yield services
    services.context.db.dispose()


def store(services: Services) -> FlakyStore:
    found = services.context.providers.store
    assert isinstance(found, FlakyStore)
    return found


def packed(services: Services) -> FulfillmentView:
    view = services.work.next(DANA).fulfillment
    assert view is not None
    pack_everything(services, DANA, view)
    act(services, DANA, view.id, Action.COMPLETE_ORDER)
    return view


def jobs(services: Services) -> list[Job]:
    with services.context.db.session() as session:
        return list(session.scalars(select(Job).order_by(Job.id)))


def test_a_lost_answer_is_read_back_and_never_written_twice(
    flaky: Services, clock: ManualClock
) -> None:
    store(flaky).lose_next_answer = True
    view = packed(flaky)
    settle(flaky, clock)
    order = store(flaky).get_order(view.orders[0].id)
    transmits = [
        s for s in order.statuses if s.title == "הועבר ל-Cargo" and OWN_MARKER in (s.comment or "")
    ]
    assert len(transmits) == 1
    assert store(flaky).writes.count((view.orders[0].id, "הועבר ל-Cargo")) == 1
    assert flaky.work.show(DANA, view.id).shipment is not None


def test_a_status_konimbo_does_not_know_is_reported(flaky: Services, clock: ManualClock) -> None:
    store(flaky).ignored_titles.add("הועבר ל-Cargo")
    view = packed(flaky)
    settle(flaky, clock)
    failed = [job for job in jobs(flaky) if job.state == JobState.FAILED]
    assert [job.last_error for job in failed] == ["konimbo_status_unknown"]
    assert steps(flaky.work.show(DANA, view.id))["shipment"] in ("failed", "waiting", "blocked")


def test_two_numbers_in_the_history_are_left_to_a_person(
    flaky: Services, clock: ManualClock
) -> None:
    store(flaky).extra_comments = ["מספר משלוח 77712345"]
    view = packed(flaky)
    settle(flaky, clock)
    shown = flaky.work.show(DANA, view.id)
    assert shown.shipment is None
    assert any(job.last_error == "shipment_number_ambiguous" for job in jobs(flaky))

    flaky.shipments.link(DANA, view.id, "77712345")
    linked = flaky.work.show(DANA, view.id)
    assert linked.shipment is not None and linked.shipment.external_id == "77712345"


def test_no_number_after_the_last_attempt_asks_for_a_manual_link(
    flaky: Services, clock: ManualClock
) -> None:
    store(flaky).cargo_integration = False
    view = packed(flaky)
    settle(flaky, clock, rounds=40, step=300)
    assert any(job.last_error == "shipment_not_in_store" for job in jobs(flaky))
    assert flaky.work.show(DANA, view.id).state is FulfillmentState.PACKED


def test_one_shipment_number_belongs_to_one_order(flaky: Services, clock: ManualClock) -> None:
    first = packed(flaky)
    settle(flaky, clock)
    number = flaky.work.show(DANA, first.id).shipment
    assert number is not None and number.external_id
    store(flaky).cargo_integration = False
    second = packed(flaky)
    with pytest.raises(ServiceError) as error:
        flaky.shipments.link(DANA, second.id, number.external_id)
    assert error.value.code == "shipment_exists"


def test_a_label_for_another_shipment_is_never_printed(flaky: Services, clock: ManualClock) -> None:
    view = packed(flaky)
    settle(flaky, clock)
    flaky.context.providers.carrier = WrongLabelCarrier()
    with pytest.raises(ServiceError) as error:
        act(flaky, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    assert error.value.code == "label_mismatch"
    printer = flaky.context.providers.printer
    assert isinstance(printer, UncertainPrinter) and printer.printed == []


def test_an_uncertain_print_needs_an_explicit_reprint(flaky: Services, clock: ManualClock) -> None:
    view = packed(flaky)
    settle(flaky, clock)
    printer = flaky.context.providers.printer
    assert isinstance(printer, UncertainPrinter)
    printer.fail_with = "print_outcome_unknown"
    with pytest.raises(ServiceError) as first:
        act(flaky, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    assert first.value.code == "print_outcome_unknown"
    with pytest.raises(ServiceError) as second:
        act(flaky, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    assert second.value.code == "print_outcome_unknown"
    assert printer.printed == []
    act(flaky, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.REPRINT)
    assert len(printer.printed) == 1


def test_a_failed_print_can_simply_be_tried_again(flaky: Services, clock: ManualClock) -> None:
    view = packed(flaky)
    settle(flaky, clock)
    printer = flaky.context.providers.printer
    assert isinstance(printer, UncertainPrinter)
    printer.fail_with = "printer_unavailable"
    with pytest.raises(ServiceError):
        act(flaky, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    act(flaky, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    assert len(printer.printed) == 1


def live_settings(tmp_path: Path) -> Settings:
    values: dict[str, Any] = {
        "mode": "live",
        "auth_url": "http://auth.internal",
        "auth_service_key": "k" * 40,
        "postgres_host": "db",
        "postgres_database": "delivery",
        "postgres_user": "delivery",
        "postgres_password": "not-used-sqlite-instead",
        "data_dir": tmp_path,
        "worker_enabled": False,
        "log_level": "WARNING",
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]  # pydantic-settings option


def test_test_mode_writes_nothing_to_konimbo(tmp_path: Path, clock: ManualClock) -> None:
    settings = live_settings(tmp_path)
    db = Database(tmp_path / "live.sqlite3")
    db.migrate()
    context = Context(settings=settings, db=db, providers=Providers(), clock=clock)
    context.providers.store = FlakyStore(clock, context.config)
    services = build_services(context)
    try:
        assert context.store_dry_run
        services.sync.run()
        view = packed(services)
        settle(services, clock)
        finished = services.work.show(DANA, view.id)
        assert finished.state is FulfillmentState.COMPLETED
        assert store(services).writes == []
        assert all(
            job.dry_run
            for job in jobs(services)
            if job.kind.startswith("konimbo.") and job.payload.get("title")
        )
        # Without Cargo and teva-printer, the label step says so instead of blocking.
        assert steps(finished)["label"] == "skipped"
    finally:
        db.dispose()
