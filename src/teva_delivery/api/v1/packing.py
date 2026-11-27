"""The packing screen: the queue, the current order, its actions and voice commands.

Service calls are blocking database work, so these handlers are plain functions, which FastAPI
runs in its thread pool. Sign-in is checked first, asynchronously.
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile

from ...domain.enums import ActionSource, Category
from ...domain.errors import ServiceError
from ...services.packing import ActionInput
from ...services.views import (
    ActionResult,
    FulfillmentView,
    QueueView,
    VoiceResult,
    WorkView,
)
from ...services.voice import MAX_AUDIO_BYTES
from ..dependencies import Actor, Credentials, ServicesDep
from .schemas import KEY_PATTERN, ActionBody, ChooseBody, SkipBody, VoiceTextBody

router = APIRouter()


@router.get("/queue")
def queue(services: ServicesDep, actor: Actor, category: Category | None = None) -> QueueView:
    return services.work.queue(actor, category)


@router.get("/work")
def current_work(services: ServicesDep, actor: Actor) -> WorkView:
    """The order this employee is packing, if any, and where it stands in their queue."""
    return services.work.current(actor)


@router.post("/work/next")
def next_order(services: ServicesDep, actor: Actor) -> WorkView:
    return services.work.next(actor)


@router.post("/work/previous")
def previous_order(services: ServicesDep, actor: Actor) -> WorkView:
    return services.work.previous(actor)


@router.post("/work/choose")
def choose(services: ServicesDep, actor: Actor, body: ChooseBody) -> WorkView:
    """Work through one category, a batch of chosen orders, or everything."""
    return services.work.choose(actor, category=body.category, batch=body.batch)


@router.get("/fulfillments/{fulfillment_id}")
def show(services: ServicesDep, actor: Actor, fulfillment_id: int) -> FulfillmentView:
    return services.work.show(actor, fulfillment_id)


@router.post("/fulfillments/{fulfillment_id}/open")
def open_order(services: ServicesDep, actor: Actor, fulfillment_id: int) -> WorkView:
    return services.work.open(actor, fulfillment_id)


@router.post("/fulfillments/{fulfillment_id}/heartbeat", status_code=204)
def heartbeat(services: ServicesDep, actor: Actor, fulfillment_id: int) -> Response:
    """Keeps the order reserved for this employee while the screen is open."""
    services.work.heartbeat(actor, fulfillment_id)
    return Response(status_code=204)


@router.post("/fulfillments/{fulfillment_id}/release")
def release(services: ServicesDep, actor: Actor, fulfillment_id: int) -> WorkView:
    return services.work.release(actor, fulfillment_id)


@router.post("/fulfillments/{fulfillment_id}/skip")
def skip(services: ServicesDep, actor: Actor, fulfillment_id: int, body: SkipBody) -> WorkView:
    return services.work.skip(actor, fulfillment_id, body.reason)


@router.post("/fulfillments/{fulfillment_id}/actions")
def act(
    services: ServicesDep,
    actor: Actor,
    credentials: Credentials,
    fulfillment_id: int,
    body: ActionBody,
) -> ActionResult:
    """One validated action, the same ones voice commands run."""
    request = ActionInput(
        action=body.action,
        line_id=body.line_id,
        note_id=body.note_id,
        quantity=body.quantity,
        everything=body.everything,
        reason=body.reason,
        note_kind=body.note_kind,
        print_mode=body.print_mode,
        source=ActionSource.MANUAL,
        idempotency_key=body.idempotency_key,
    )
    return services.work.act(
        actor, fulfillment_id, request, credentials=credentials, auto_next=body.auto_next
    )


@router.post("/voice/text")
def voice_text(
    services: ServicesDep, actor: Actor, credentials: Credentials, body: VoiceTextBody
) -> VoiceResult:
    return services.voice.text(
        actor,
        body.fulfillment_id,
        body.transcript,
        utterance_id=body.utterance_id,
        confidence=body.confidence,
        credentials=credentials,
    )


@router.post("/voice/audio")
def voice_audio(
    services: ServicesDep,
    actor: Actor,
    credentials: Credentials,
    audio: Annotated[UploadFile, File()],
    utterance_id: Annotated[str, Form(pattern=KEY_PATTERN)],
    fulfillment_id: Annotated[int | None, Form(ge=1)] = None,
) -> VoiceResult:
    """A short recording, recognised by the local speech service. Audio is not stored."""
    data = audio.file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise ServiceError("audio_too_large")
    return services.voice.audio(
        actor,
        fulfillment_id,
        data,
        audio.content_type or "",
        utterance_id=utterance_id,
        credentials=credentials,
    )
