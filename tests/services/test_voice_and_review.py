"""Voice commands, the AI fallback, duplicate review and following Konimbo."""

import pytest

from teva_delivery.core.clock import ManualClock
from teva_delivery.domain.enums import FulfillmentState, ReviewDecision
from teva_delivery.domain.errors import ServiceError
from teva_delivery.domain.ports import ProposedAction
from teva_delivery.providers.demo import DemoStore
from teva_delivery.services.container import Services
from teva_delivery.services.views import FulfillmentView, VoiceResult

from ..fakes import ScriptedInterpreter
from ..support import ADMIN, DANA, NO_CREDENTIALS, settle


@pytest.fixture
def opened(services: Services) -> tuple[Services, FulfillmentView]:
    """The consolidation order: סרום שפע, סרום ויטמין C כתום and two שמן ארגן."""
    services.sync.run()
    view = services.work.next(DANA).fulfillment
    assert view is not None
    return services, view


counter = iter(range(10_000, 99_999))


def say(
    services: Services,
    view: FulfillmentView,
    text: str,
    *,
    utterance: str | None = None,
    confidence: float | None = 0.9,
) -> VoiceResult:
    return services.voice.text(
        DANA,
        view.id,
        text,
        utterance_id=utterance or f"utt-{next(counter)}",
        confidence=confidence,
        credentials=NO_CREDENTIALS,
    )


def confirmed(result: VoiceResult, title_start: str) -> int:
    assert result.fulfillment is not None
    return next(
        line.confirmed for line in result.fulfillment.lines if line.title.startswith(title_start)
    )


def test_a_spoken_product_is_confirmed(opened: tuple[Services, FulfillmentView]) -> None:
    services, view = opened
    result = say(services, view, "שמתי שמן ארגן")
    assert result.outcome == "executed" and result.interpreter == "rules"
    assert confirmed(result, "שמן ארגן") == 1
    assert "שמן ארגן" in result.speech


def test_an_ambiguous_product_is_asked_about_then_answered(
    opened: tuple[Services, FulfillmentView],
) -> None:
    services, view = opened
    question = say(services, view, "שמתי סרום")
    assert question.outcome == "clarify"
    assert "?" in question.speech
    answer = say(services, view, "הכתום")
    assert answer.outcome == "executed"
    assert confirmed(answer, "סרום ויטמין") == 1
    assert confirmed(answer, "סרום שפע") == 0


def test_a_repeated_request_is_not_run_twice(
    opened: tuple[Services, FulfillmentView], clock: ManualClock
) -> None:
    services, view = opened
    first = say(services, view, "שמתי שמן ארגן", utterance="utt-same-0001")
    replay = say(services, view, "שמתי שמן ארגן", utterance="utt-same-0001")
    assert replay.outcome == first.outcome == "executed"
    echo = say(services, view, "שמתי שמן ארגן")
    assert echo.outcome == "duplicate"
    clock.advance(10)
    again = say(services, view, "שמתי שמן ארגן")
    assert again.outcome == "executed"
    assert confirmed(again, "שמן ארגן") == 2


def test_unclear_speech_and_chatter(opened: tuple[Services, FulfillmentView]) -> None:
    services, view = opened
    assert say(services, view, "שמתי שמן ארגן", confidence=0.2).outcome == "low_confidence"
    assert say(services, view, "מה נשמע אחי, איך היה אתמול").outcome == "ignored"
    remaining = say(services, view, "מה עוד חסר?")
    assert remaining.outcome == "executed" and "שמן ארגן" in remaining.speech
    assert say(services, view, "עזרה").outcome == "help"


def test_voice_can_complete_and_report_missing(opened: tuple[Services, FulfillmentView]) -> None:
    services, view = opened
    missing = say(services, view, "חסר לנו שמן ארגן")
    assert missing.outcome == "executed"
    assert missing.fulfillment is not None
    line = next(line for line in missing.fulfillment.lines if line.title.startswith("שמן ארגן"))
    assert line.missing == 2
    undone = say(services, view, "תבטל את המוצר האחרון")
    assert undone.outcome == "executed"


def test_the_ai_names_a_product_the_rules_could_not(
    opened: tuple[Services, FulfillmentView],
) -> None:
    services, view = opened
    oil = next(line for line in view.lines if line.title.startswith("שמן ארגן"))
    interpreter = ScriptedInterpreter(ProposedAction("confirm_product", product_id=oil.id))
    services.context.providers.interpreter = interpreter
    result = say(services, view, "שמתי את הבקבוק הזהוב")
    assert interpreter.asked == ["שמתי את הבקבוק הזהוב"]
    assert result.outcome == "executed" and result.interpreter == "ai"
    assert confirmed(result, "שמן ארגן") == 1


def test_what_the_ai_proposes_is_checked(opened: tuple[Services, FulfillmentView]) -> None:
    services, view = opened
    services.context.providers.interpreter = ScriptedInterpreter(
        ProposedAction("confirm_product", product_id=999_999),
        ProposedAction("complete_order"),
    )
    result = say(services, view, "שמתי את הבקבוק הזהוב")
    assert result.outcome != "executed"
    assert result.fulfillment is None or result.fulfillment.progress.units_confirmed == 0


def test_an_accidental_duplicate_is_cancelled_only_by_a_person(services: Services) -> None:
    services.sync.run()
    review = services.duplicates.reviews()[0]
    assert review.blocking
    assert all(order.state is FulfillmentState.REVIEW for order in review.orders)
    assert all(order.masked_phone and "3216" not in order.masked_phone for order in review.orders)
    suspect = next(order for order in review.orders if order.suspect)
    decided = services.duplicates.decide(DANA, review.id, ReviewDecision.MARK_DUPLICATE)
    states = {order.id: order.state for order in decided.orders}
    assert states[suspect.id] is FulfillmentState.CANCELLED
    assert [state for key, state in states.items() if key != suspect.id] == [FulfillmentState.READY]
    with pytest.raises(ServiceError) as again:
        services.duplicates.decide(DANA, review.id, ReviewDecision.KEEP_SEPARATE)
    assert again.value.code == "review_decided"


def test_orders_kept_separate_both_enter_the_queue(services: Services) -> None:
    services.sync.run()
    review = services.duplicates.reviews()[0]
    decided = services.duplicates.decide(DANA, review.id, ReviewDecision.KEEP_SEPARATE)
    assert {order.state for order in decided.orders} == {FulfillmentState.READY}


def test_consolidating_packs_both_orders_in_one_box(services: Services) -> None:
    services.sync.run()
    review = services.duplicates.reviews()[0]
    decided = services.duplicates.decide(DANA, review.id, ReviewDecision.CONSOLIDATE)
    keeper = next(order for order in decided.orders if order.state is FulfillmentState.READY)
    assert keeper.fulfillment_id is not None
    merged = services.work.show(DANA, keeper.fulfillment_id)
    assert len(merged.orders) == 2
    assert sum(line.required for line in merged.lines) == 6


def demo_store(services: Services) -> DemoStore:
    found = services.context.providers.store
    assert isinstance(found, DemoStore)
    return found


def test_an_order_the_office_cancels_leaves_the_queue(services: Services) -> None:
    services.sync.run()
    store = demo_store(services)
    store.append_status("20002", title="בוטל", comment="הלקוח ביטל", username="משרד")
    services.sync.run()
    queue = services.work.queue(DANA)
    assert all("20002" not in item.order_ids for item in queue.items)


def test_a_line_removed_in_konimbo_is_flagged(services: Services) -> None:
    services.sync.run()
    store = demo_store(services)
    before = store.get_order("20002")
    items = [
        {
            "id": int(line.item_id or 0),
            "title": line.title,
            "code": line.sku,
            "quantity": line.quantity,
        }
        for line in before.lines[1:]
    ]
    store.edit("20002", items=items)
    services.sync.run()
    queue = services.work.queue(DANA)
    item = next(item for item in queue.items if "20002" in item.order_ids)
    view = services.work.show(DANA, item.id)
    assert [line.removed for line in view.lines].count(True) == 1


def test_settings_change_at_runtime(services: Services) -> None:
    with pytest.raises(ServiceError) as denied:
        services.overview.update_settings(DANA, {"status_urgent": "דחוף"})
    assert denied.value.code == "admin_required"
    updated = services.overview.update_settings(ADMIN, {"status_urgent": "דחוף"})
    assert updated.config.status_urgent == "דחוף"
    assert services.context.config().status_urgent == "דחוף"
    with pytest.raises(ServiceError) as invalid:
        services.overview.update_settings(ADMIN, {"status_urgent": "ממתין למלאי"})
    assert invalid.value.code == "settings_invalid"


def test_status_names_are_learned_from_konimbo(services: Services, clock: ManualClock) -> None:
    services.sync.run()
    titles = {option.title for option in services.overview.settings().status_options}
    assert {"הזמנה חדשה", "מיוחדים דחופים", "נקודות איסוף"} <= titles
    settle(services, clock, rounds=1)
    assert services.overview.discover_statuses(ADMIN).status_options
    assert services.overview.info().konimbo.ready
