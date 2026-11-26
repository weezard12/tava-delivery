"""The background worker, choosing what to pack, and the less common packing actions."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from teva_delivery.core.clock import ManualClock
from teva_delivery.domain.enums import (
    Action,
    Category,
    FulfillmentState,
    JobKind,
    JobState,
    NoteKind,
)
from teva_delivery.domain.errors import ServiceError
from teva_delivery.persistence.models import ActionRequest, Job, Utterance
from teva_delivery.services.container import Services
from teva_delivery.services.records import enqueue
from teva_delivery.services.worker import Task, Worker, purge

from ..support import ADMIN, DANA, NO_CREDENTIALS, YOSSI, act, pack_everything, settle


def add_job(
    services: Services,
    kind: JobKind = JobKind.KONIMBO_NOTE,
    attempts: int = 3,
    order_id: str = "20001",
) -> int:
    with services.context.db.session(write=True) as session:
        job = enqueue(
            session,
            kind,
            f"test:{kind}:{attempts}",
            {"order_id": order_id, "title": "הערת מחסן", "comment": "x", "marker": "[מחסן #t]"},
            now=services.context.now(),
            max_attempts=attempts,
            order_id=order_id,
        )
        session.flush()
        return job.id


def job_state(services: Services, job_id: int) -> Job:
    with services.context.db.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def test_a_job_whose_worker_stopped_is_picked_up_again(
    services: Services, clock: ManualClock
) -> None:
    job_id = add_job(services)
    assert services.worker.claim() == job_id
    assert services.worker.claim() is None
    clock.advance(services.context.settings.lease_seconds + 1)
    assert services.worker.claim() == job_id
    assert job_state(services, job_id).attempts == 2


def test_a_job_out_of_attempts_is_left_for_a_person(services: Services, clock: ManualClock) -> None:
    note = add_job(services, attempts=1)
    shipment = add_job(services, JobKind.CARGO_CREATE_SHIPMENT, attempts=1, order_id="20002")
    for _ in range(2):
        services.worker.claim()
    clock.advance(services.context.settings.lease_seconds + 1)
    assert services.worker.claim() is None
    assert job_state(services, note).state == JobState.FAILED
    # A shipment request that may have reached Cargo is never simply failed.
    assert job_state(services, shipment).state == JobState.UNCERTAIN


def test_writes_for_one_order_run_in_order(services: Services) -> None:
    first = add_job(services, attempts=3)
    second = add_job(services, attempts=4)
    assert services.worker.claim() == first
    assert services.worker.claim() is None
    with services.context.db.session(write=True) as session:
        job = session.get(Job, first)
        assert job is not None
        job.state = JobState.SUCCEEDED
    assert services.worker.claim() == second


def test_a_crashing_handler_releases_its_job(services: Services, clock: ManualClock) -> None:
    def crash(job_id: int) -> None:
        raise RuntimeError("bug")

    worker = Worker(services.context, {JobKind.KONIMBO_NOTE: crash}, [])
    job_id = add_job(services)
    assert worker.run_once() == 1
    job = job_state(services, job_id)
    assert (job.state, job.last_error) == (JobState.PENDING, "internal_error")


def test_periodic_tasks_run_when_due(services: Services, clock: ManualClock) -> None:
    runs: list[str] = []
    worker = Worker(
        services.context,
        {},
        [Task("tick", 60, lambda: runs.append("tick")), Task("off", 0, lambda: runs.append("off"))],
    )
    worker.run_tasks()
    worker.run_tasks()
    clock.advance(61)
    worker.run_tasks()
    assert runs == ["tick", "tick"]


def test_the_worker_thread_starts_and_stops(services: Services) -> None:
    ran = threading.Event()
    worker = Worker(services.context, {}, [Task("tick", 3600, ran.set)])
    worker.start()
    worker.start()
    assert ran.wait(5)
    worker.stop()


def test_old_transcripts_and_requests_are_forgotten(services: Services, clock: ManualClock) -> None:
    services.sync.run()
    view = services.work.next(DANA).fulfillment
    assert view is not None
    services.voice.text(
        DANA,
        view.id,
        "מה עוד חסר?",
        utterance_id="utt-old-0001",
        confidence=None,
        credentials=NO_CREDENTIALS,
    )
    act(
        services,
        DANA,
        view.id,
        Action.CONFIRM_PRODUCT,
        line_id=view.lines[0].id,
        idempotency_key="old-key-0001",
    )
    clock.advance(timedelta(days=31).total_seconds())
    assert purge(services.context) >= 2
    with services.context.db.session() as session:
        assert session.scalars(select(Utterance)).first() is None
        assert session.scalars(select(ActionRequest)).first() is None


def test_choosing_a_category_or_a_batch(services: Services) -> None:
    services.sync.run()
    urgent = services.work.choose(DANA, category=Category.URGENT, batch=None)
    assert urgent.position.category is Category.URGENT
    first = services.work.next(DANA).fulfillment
    assert first is not None and first.category is Category.URGENT
    queue = services.work.queue(DANA, Category.REGULAR)
    regular = [
        item.id
        for item in queue.items
        if item.category is Category.REGULAR and item.state is FulfillmentState.READY
    ]
    batch = list(reversed(regular))
    chosen = services.work.choose(DANA, category=None, batch=batch)
    assert chosen.position.batch
    nxt = services.work.next(DANA).fulfillment
    assert nxt is not None and nxt.id == batch[0]
    with pytest.raises(ServiceError) as empty:
        services.work.choose(DANA, category=None, batch=[])
    assert empty.value.code == "batch_empty"


def test_releasing_and_heartbeats(services: Services, clock: ManualClock) -> None:
    services.sync.run()
    view = services.work.next(DANA).fulfillment
    assert view is not None
    services.work.heartbeat(DANA, view.id)
    with pytest.raises(ServiceError) as locked:
        services.work.heartbeat(YOSSI, view.id)
    assert locked.value.code == "fulfillment_locked"
    services.work.release(DANA, view.id)
    assert services.work.show(DANA, view.id).state is FulfillmentState.READY
    assert services.work.current(DANA).fulfillment is None


def test_note_kinds_and_unconfirming(services: Services) -> None:
    services.sync.run()
    view = services.work.next(DANA).fulfillment
    assert view is not None
    note = view.notes[0]
    act(services, DANA, view.id, Action.CONFIRM_ORDER_NOTE, note_id=note.id)
    reverted = act(services, DANA, view.id, Action.UNCONFIRM_ORDER_NOTE, note_id=note.id)
    assert reverted.fulfillment is not None and not reverted.fulfillment.notes[0].confirmed
    info = act(
        services, DANA, view.id, Action.SET_NOTE_KIND, note_id=note.id, note_kind=NoteKind.INFO
    )
    assert info.fulfillment is not None
    assert info.fulfillment.notes[0].kind is NoteKind.INFO
    assert info.fulfillment.progress.notes_open == 0


def test_missing_can_be_undone(services: Services) -> None:
    services.sync.run()
    view = services.work.next(DANA).fulfillment
    assert view is not None
    line = view.lines[0]
    act(services, DANA, view.id, Action.REPORT_MISSING_INVENTORY, line_id=line.id)
    with pytest.raises(ServiceError) as blocked:
        act(services, DANA, view.id, Action.CONFIRM_PRODUCT, line_id=line.id)
    assert blocked.value.code == "line_missing"
    cleared = act(services, DANA, view.id, Action.UNDO_MISSING_INVENTORY, line_id=line.id)
    assert cleared.fulfillment is not None and cleared.fulfillment.lines[0].missing == 0


def test_reopening_before_and_after_pickup(services: Services, clock: ManualClock) -> None:
    services.sync.run()
    view = services.work.next(DANA).fulfillment
    assert view is not None
    pack_everything(services, DANA, view)
    act(services, DANA, view.id, Action.COMPLETE_ORDER)
    reopened = act(services, DANA, view.id, Action.REOPEN_ORDER)
    assert reopened.fulfillment is not None
    assert reopened.fulfillment.state is FulfillmentState.PACKING
    act(services, DANA, view.id, Action.COMPLETE_ORDER)
    settle(services, clock)
    act(services, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode="manual")
    settle(services, clock)
    with pytest.raises(ServiceError) as finished:
        act(services, DANA, view.id, Action.REOPEN_ORDER)
    assert finished.value.code == "order_finished"


def test_failed_konimbo_jobs_can_be_retried_by_an_admin(
    services: Services, clock: ManualClock
) -> None:
    job_id = add_job(services, attempts=1)
    services.worker.claim()
    clock.advance(services.context.settings.lease_seconds + 1)
    services.worker.claim()
    assert job_state(services, job_id).state == JobState.FAILED
    services.jobs.retry(ADMIN, job_id)
    assert job_state(services, job_id).state == JobState.PENDING
    with pytest.raises(ServiceError) as missing:
        services.jobs.retry(ADMIN, 99_999)
    assert missing.value.code == "job_not_found"
