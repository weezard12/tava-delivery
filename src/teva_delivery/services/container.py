"""Wires the services together. The API and the worker share one container."""

from dataclasses import dataclass

from ..domain.enums import JobKind
from .aliases import Aliases
from .completion import Completion
from .context import Context
from .duplicates import Duplicates
from .jobs import StoreJobs
from .overview import Overview
from .packing import Packing
from .presenter import Presenter
from .printing import Printing
from .shipments import Shipments
from .sync import Sync
from .voice import Voice
from .work import Work
from .worker import Task, Worker, purge


@dataclass(slots=True)
class Services:
    context: Context
    completion: Completion
    presenter: Presenter
    shipments: Shipments
    jobs: StoreJobs
    packing: Packing
    printing: Printing
    work: Work
    voice: Voice
    sync: Sync
    duplicates: Duplicates
    aliases: Aliases
    overview: Overview
    worker: Worker


def build_services(context: Context) -> Services:
    completion = Completion(context)
    presenter = Presenter(context, completion)
    shipments = Shipments(context, completion)
    jobs = StoreJobs(context, completion, shipments)
    packing = Packing(context, completion, presenter)
    printing = Printing(context, completion, presenter)
    work = Work(context, completion, presenter, packing, printing)
    voice = Voice(context, work, presenter)
    sync = Sync(context)
    settings = context.settings
    worker = Worker(
        context,
        {
            JobKind.KONIMBO_STATUS: jobs.run_write,
            JobKind.KONIMBO_NOTE: jobs.run_write,
            JobKind.KONIMBO_FIND_SHIPMENT: jobs.run_find,
            JobKind.CARGO_CREATE_SHIPMENT: shipments.run_create,
        },
        [
            Task("konimbo.import", settings.sync_interval_seconds, sync.run),
            Task("cargo.status", settings.cargo_status_sync_minutes * 60, shipments.sync_statuses),
            Task("work.pause_abandoned", 300, work.pause_abandoned),
            Task("purge", 3600, lambda: purge(context)),
        ],
    )
    return Services(
        context=context,
        completion=completion,
        presenter=presenter,
        shipments=shipments,
        jobs=jobs,
        packing=packing,
        printing=printing,
        work=work,
        voice=voice,
        sync=sync,
        duplicates=Duplicates(context),
        aliases=Aliases(context),
        overview=Overview(context, sync),
        worker=worker,
    )
