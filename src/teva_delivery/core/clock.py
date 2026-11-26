"""The service's only source of the current time, so tests can move it."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ManualClock:
    """A clock for tests: it stands still until ``advance`` moves it."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 11, 26, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> datetime:
        self.current += timedelta(seconds=seconds)
        return self.current
