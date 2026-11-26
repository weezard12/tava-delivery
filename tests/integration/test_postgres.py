"""The same flows on PostgreSQL, where row locks keep two packers off one order.

Run with a throwaway database: ``TEVA_TEST_POSTGRES_URL=postgresql+psycopg://user@host/db
pytest -m postgres``. The test drops and recreates the ``public`` schema.
"""

import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import make_url, text

from teva_delivery.core.clock import ManualClock
from teva_delivery.domain.enums import Action, FulfillmentState, PrintMode
from teva_delivery.domain.identity import Principal
from teva_delivery.persistence.database import Database
from teva_delivery.providers.factory import build_providers
from teva_delivery.services.container import Services, build_services
from teva_delivery.services.context import Context, Providers

from ..support import DANA, act, make_settings, pack_everything, settle

URL = os.environ.get("TEVA_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="TEVA_TEST_POSTGRES_URL is not set"),
]


@pytest.fixture
def postgres(tmp_path: Path, clock: ManualClock) -> Iterator[Services]:
    db = Database(make_url(URL))
    with db.engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    db.migrate()
    db.migrate()  # A second start finds nothing to do.
    settings = make_settings(tmp_path)
    context = Context(settings=settings, db=db, providers=Providers(), clock=clock)
    context.providers = build_providers(settings, clock, context.config)
    services = build_services(context)
    services.sync.run()
    yield services
    db.dispose()


def test_an_order_from_the_queue_to_pickup(postgres: Services, clock: ManualClock) -> None:
    view = postgres.work.next(DANA).fulfillment
    assert view is not None
    pack_everything(postgres, DANA, view)
    act(postgres, DANA, view.id, Action.COMPLETE_ORDER)
    settle(postgres, clock)
    act(postgres, DANA, view.id, Action.PRINT_SHIPPING_LABEL, print_mode=PrintMode.MANUAL)
    settle(postgres, clock)
    assert postgres.work.show(DANA, view.id).state is FulfillmentState.COMPLETED


def test_packers_asking_at_the_same_moment_get_different_orders(postgres: Services) -> None:
    packers = [Principal(f"packer-{index}") for index in range(6)]
    taken: dict[str, int | None] = {}
    start = threading.Barrier(len(packers))

    def take(packer: Principal) -> None:
        start.wait()
        view = postgres.work.next(packer).fulfillment
        taken[packer.id] = view.id if view else None

    threads = [threading.Thread(target=take, args=(packer,)) for packer in packers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    ids = [value for value in taken.values() if value is not None]
    assert len(ids) == len(set(ids)) == len(packers)
