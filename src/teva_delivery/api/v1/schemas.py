"""Request bodies. Responses are the service views (``services/views.py``)."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import Action, Category, NoteKind, PrintMode, ReviewDecision

KEY_PATTERN = r"^[A-Za-z0-9_-]{8,80}$"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionBody(Body):
    """One packing action from a button. Voice commands become the same actions."""

    action: Action
    line_id: int | None = Field(default=None, ge=1)
    note_id: int | None = Field(default=None, ge=1)
    quantity: int | None = Field(default=None, ge=1, le=10_000)
    everything: bool = False
    reason: str | None = Field(default=None, max_length=300)
    note_kind: NoteKind | None = None
    print_mode: PrintMode | None = None
    # The same key sent twice (a double tap, a retried request) runs the action once.
    idempotency_key: str | None = Field(default=None, pattern=KEY_PATTERN)
    # After completing, open the next order.
    auto_next: bool = False


class SkipBody(Body):
    reason: str = Field(min_length=1, max_length=300)


class ChooseBody(Body):
    category: Category | None = None
    batch: list[int] | None = Field(default=None, max_length=100)


class VoiceTextBody(Body):
    """A sentence the browser recognised itself, or typed into the voice box."""

    transcript: str = Field(max_length=500)
    utterance_id: str = Field(pattern=KEY_PATTERN)
    fulfillment_id: int | None = Field(default=None, ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)


class LinkShipmentBody(Body):
    number: str = Field(min_length=6, max_length=20, pattern=r"^\s*\d+\s*$")


class BatchBody(Body):
    fulfillment_ids: list[int] = Field(min_length=1, max_length=100)


class DecisionBody(Body):
    decision: ReviewDecision
    # For mark_duplicate: the Konimbo order numbers that are the duplicates.
    duplicates: list[str] | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=300)


class AliasBody(Body):
    line_id: int = Field(ge=1)
    alias: str = Field(min_length=2, max_length=100)


class SettingsBody(Body):
    """Only the fields that change. Unknown fields are refused."""

    changes: dict[str, Any] = Field(min_length=1, max_length=30)
