"""Shared fixtures: settings, a manual clock and the service container over a temporary database."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from teva_delivery.core.clock import ManualClock
from teva_delivery.services.container import Services

from .support import START, build, make_settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service reads DELIVERY_* variables; a developer's own must not leak into tests."""
    for name in list(os.environ):
        if name.startswith("DELIVERY_"):
            monkeypatch.delenv(name)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def services(tmp_path: Path, clock: ManualClock) -> Iterator[Services]:
    """The demo store, Cargo and printer, as demo mode runs them."""
    built = build(make_settings(tmp_path), clock)
    yield built
    built.context.db.dispose()
