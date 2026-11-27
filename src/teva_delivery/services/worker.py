"""The background worker: runs the outbox jobs and the periodic tasks, in one thread.

Jobs are claimed with a lease. Jobs for one Konimbo order run one at a time, in the order
they were recorded, so "awaiting inventory" never overtakes "packing started". A job whose
lease ran out (the process stopped mid-way) is picked up again and its handler decides what
is safe: a write that may have been sent is read back before anything is sent again.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..domain.enums import ACTIVE_JOB_STATES, JobKind, JobState
from ..persistence.models import ActionRequest, Job, Utterance
from .context import Context

logger = logging.getLogger("teva_delivery.worker")

JOBS_PER_ROUND = 10


@dataclass(slots=True)
class Task:
    name: str
    interval: float
    run: Callable[[], object]
    due: datetime | None = field(default=None)


class Worker:
    def __init__(
        self,
        context: Context,
        handlers: dict[JobKind, Callable[[int], None]],
        tasks: list[Task],
    ) -> None:
        self.context = context
        self.handlers = handlers
        self.tasks = [task for task in tasks if task.interval > 0]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="teva-delivery-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                logger.exception("The worker round failed")
                worked = 0
            if not worked:
                self._stop.wait(self.context.settings.worker_poll_seconds)

    def run_once(self) -> int:
        """One round: due periodic tasks, then up to a few due jobs. Returns jobs run."""
        self.run_tasks()
        ran = 0
        for _ in range(JOBS_PER_ROUND):
            job_id = self.claim()
            if job_id is None:
                break
            self.run_job(job_id)
            ran += 1
        return ran

    def drain(self, limit: int = 200) -> int:
        """Run jobs until none is due (tests and the demo)."""
        ran = 0
        while ran < limit:
            job_id = self.claim()
            if job_id is None:
                break
            self.run_job(job_id)
            ran += 1
        return ran

    def run_tasks(self) -> None:
        now = self.context.now()
        for task in self.tasks:
            if task.due is not None and task.due > now:
                continue
            task.due = now + timedelta(seconds=task.interval)
            try:
                task.run()
            except Exception:
                logger.exception("Periodic task failed", extra={"event": {"task": task.name}})

    def claim(self) -> int | None:
        context = self.context
        with context.db.session(write=True) as session:
            now = context.now()
            due = session.scalars(
                select(Job)
                .where(
                    or_(
                        and_(Job.state == JobState.PENDING, Job.run_after <= now),
                        and_(Job.state == JobState.RUNNING, Job.lease_until < now),
                    )
                )
                .order_by(Job.id)
                .limit(50)
                .with_for_update(skip_locked=True)
            ).all()
            for job in due:
                if job.order_id is not None and self._waits_for_earlier(session, job):
                    continue
                if job.attempts >= job.max_attempts:
                    # Out of attempts while running: the handler never finished.
                    job.state = (
                        JobState.UNCERTAIN
                        if job.kind == JobKind.CARGO_CREATE_SHIPMENT
                        else JobState.FAILED
                    )
                    job.last_error = job.last_error or "job_abandoned"
                    job.lease_until = None
                    job.finished_at = now
                    job.updated_at = now
                    continue
                job.state = JobState.RUNNING
                job.attempts += 1
                job.lease_until = now + timedelta(seconds=context.settings.lease_seconds)
                job.updated_at = now
                return job.id
        return None

    def _waits_for_earlier(self, session: Session, job: Job) -> bool:
        """Konimbo writes for one order run in order. Reading the shipment back never waits."""
        if job.kind == JobKind.KONIMBO_FIND_SHIPMENT:
            return False
        earlier = session.scalars(
            select(Job.id)
            .where(
                Job.order_id == job.order_id,
                Job.id < job.id,
                Job.state.in_(list(ACTIVE_JOB_STATES)),
                Job.kind != JobKind.KONIMBO_FIND_SHIPMENT,
            )
            .limit(1)
        ).first()
        return earlier is not None

    def run_job(self, job_id: int) -> None:
        with self.context.db.session() as session:
            job = session.get(Job, job_id)
            kind = JobKind(job.kind) if job is not None else None
        handler = self.handlers.get(kind) if kind is not None else None
        if handler is None:
            logger.error("No handler for a job", extra={"event": {"job": job_id}})
            return
        try:
            handler(job_id)
        except Exception:
            logger.exception("A job failed unexpectedly", extra={"event": {"job": job_id}})
            self._release(job_id)

    def _release(self, job_id: int) -> None:
        """An unexpected error: try again later; the handler reads back before re-sending."""
        with self.context.db.session(write=True) as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job is None or job.state != JobState.RUNNING:
                return
            now = self.context.now()
            job.state = JobState.PENDING
            job.last_error = "internal_error"
            job.lease_until = None
            job.run_after = now + timedelta(seconds=60 * job.attempts)
            job.updated_at = now


def purge(context: Context) -> int:
    """Forget old transcripts and idempotency records."""
    now = context.now()
    removed = 0
    with context.db.session(write=True) as session:
        transcripts = now - timedelta(days=context.settings.voice_transcript_days)
        for row in session.scalars(
            select(Utterance).where(Utterance.created_at < transcripts).limit(1000)
        ):
            session.delete(row)
            removed += 1
        requests = now - timedelta(days=2)
        for request in session.scalars(
            select(ActionRequest).where(ActionRequest.created_at < requests).limit(1000)
        ):
            session.delete(request)
            removed += 1
    return removed
