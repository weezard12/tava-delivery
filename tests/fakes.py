"""Providers that misbehave on purpose, built on the demo ones."""

from typing import Any

from teva_delivery.domain.errors import ProviderError
from teva_delivery.domain.ports import (
    Interpretation,
    InterpreterContext,
    PrintCredentials,
    PrintReceipt,
    ProposedAction,
    StoreOrder,
)
from teva_delivery.providers.demo import DemoCarrier, DemoPrinter, DemoStore


class FlakyStore(DemoStore):
    """Konimbo with a lost answer, ignored statuses and no Cargo integration, on request."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.writes: list[tuple[str, str]] = []
        self.lose_next_answer = False
        self.ignored_titles: set[str] = set()
        self.extra_comments: list[str] = []

    def append_status(
        self, order_id: str, *, title: str, comment: str, username: str
    ) -> StoreOrder:
        self.writes.append((order_id, title))
        if title in self.ignored_titles:
            # Konimbo records an unknown title as history only.
            before = self.get_order(order_id).status_title
            super().append_status(order_id, title=title, comment=comment, username=username)
            self.edit(order_id, status_option_title=before)
            return self.get_order(order_id)
        result = super().append_status(order_id, title=title, comment=comment, username=username)
        for text in self.extra_comments:
            super().append_status(
                order_id, title=self._config().note_title, comment=text, username="Cargo"
            )
        self.extra_comments = []
        if self.lose_next_answer:
            self.lose_next_answer = False
            raise ProviderError("konimbo_unavailable", retryable=True, sent=True)
        return result


class WrongLabelCarrier(DemoCarrier):
    def label_url(self, external_id: str) -> str:
        return "https://api-v2.cargo.co.il/labels/demo/shipment_99999999_demo.pdf"


class UncertainPrinter(DemoPrinter):
    def __init__(self) -> None:
        super().__init__()
        self.fail_with: str | None = None

    def print_label(
        self, label_url: str, *, printer_id: str, direct: bool, credentials: PrintCredentials
    ) -> PrintReceipt:
        if self.fail_with:
            code, self.fail_with = self.fail_with, None
            raise ProviderError(code, sent=code == "print_outcome_unknown")
        return super().print_label(
            label_url, printer_id=printer_id, direct=direct, credentials=credentials
        )


class ScriptedInterpreter:
    def __init__(self, *actions: ProposedAction, unrelated: bool = False) -> None:
        self.actions = actions
        self.unrelated = unrelated
        self.asked: list[str] = []

    @property
    def name(self) -> str:
        return "scripted"

    def interpret(self, transcript: str, context: InterpreterContext) -> Interpretation:
        self.asked.append(transcript)
        return Interpretation(actions=self.actions, unrelated=self.unrelated)


class FlakyCarrier(DemoCarrier):
    """Cargo that refuses or loses the answer on request."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_with: ProviderError | None = None
        self.created = 0

    def create_shipment(self, request: Any) -> Any:
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error
        self.created += 1
        return super().create_shipment(request)
