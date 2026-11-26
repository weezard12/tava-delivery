"""The voice assistant: one spoken sentence in, validated actions out, a Hebrew answer back.

Speech is recognised by the local speech service (or the browser, as a fallback) and the
sentence is read by the deterministic Hebrew parser. The optional AI interpreter is asked
only when the parser understood that a product was meant but could not tell which one; what
it proposes is checked against the order like any other request. Nothing here changes an
order directly: every action goes through the same service as the screen's buttons.

A sentence is acted on once: each utterance has an ID (a repeat returns the first answer),
the same words again within a few seconds are taken as a repeated recognition, and a
recognition below the confidence threshold is asked again rather than guessed.
"""

import logging
import re
from datetime import timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.enums import Action, ActionSource, NoteKind
from ..domain.errors import ProviderError, ServiceError
from ..domain.hebrew.commands import (
    Clarification,
    Command,
    HelpText,
    OrderContext,
    ParseResult,
    parse,
)
from ..domain.hebrew.matcher import Target
from ..domain.hebrew.text import normalize
from ..domain.identity import Principal
from ..domain.ports import (
    InterpreterContext,
    InterpreterNote,
    InterpreterProduct,
    PrintCredentials,
    ProposedAction,
)
from ..persistence.models import Fulfillment, ProductAlias, Utterance
from .context import Context
from .packing import ActionInput
from .presenter import Presenter, active_lines, item_key
from .records import get_fulfillment
from .views import ActionResult, VoiceResult
from .work import Work

logger = logging.getLogger("teva_delivery.voice")

UTTERANCE_ID = re.compile(r"[A-Za-z0-9_-]{8,80}")
MAX_TRANSCRIPT = 500
MAX_AUDIO_BYTES = 2_000_000
CLARIFICATION_SECONDS = 45
# What the AI may propose. Navigation and completion stay with the rules and the buttons.
AI_ACTIONS = frozenset(
    {Action.CONFIRM_PRODUCT, Action.REPORT_MISSING_INVENTORY, Action.CONFIRM_ORDER_NOTE}
)
NOT_HEARD = "לא שמעתי טוב. אפשר לחזור על זה?"

type Outcome = Literal[
    "executed", "ignored", "clarify", "failed", "duplicate", "low_confidence", "help"
]


class Voice:
    def __init__(self, context: Context, work: Work, presenter: Presenter) -> None:
        self.context = context
        self.work = work
        self.presenter = presenter

    def audio(
        self,
        actor: Principal,
        fulfillment_id: int | None,
        audio: bytes,
        content_type: str,
        *,
        utterance_id: str,
        credentials: PrintCredentials,
    ) -> VoiceResult:
        transcriber = self.context.providers.transcriber
        if transcriber is None:
            raise ServiceError("stt_not_configured")
        if not audio:
            raise ServiceError("audio_invalid")
        if len(audio) > MAX_AUDIO_BYTES:
            raise ServiceError("audio_too_large")
        try:
            transcript = transcriber.transcribe(audio, content_type)
        except ProviderError as error:
            raise ServiceError(error.code) from None
        return self.text(
            actor,
            fulfillment_id,
            transcript.text,
            utterance_id=utterance_id,
            confidence=transcript.confidence,
            credentials=credentials,
        )

    def text(
        self,
        actor: Principal,
        fulfillment_id: int | None,
        transcript: str,
        *,
        utterance_id: str,
        confidence: float | None,
        credentials: PrintCredentials,
    ) -> VoiceResult:
        if not UTTERANCE_ID.fullmatch(utterance_id):
            raise ServiceError("transcript_invalid")
        transcript = transcript.strip()
        if len(transcript) > MAX_TRANSCRIPT:
            raise ServiceError("transcript_invalid")
        context = self.context
        normalized = normalize(transcript)
        with context.db.session() as session:
            earlier = session.get(Utterance, utterance_id)
            if earlier is not None:
                if earlier.actor != actor.id:
                    raise ServiceError("idempotency_conflict")
                replay = VoiceResult.model_validate(earlier.response)
                return self._attach(replay, actor)
            repeated = self._repeated(session, actor, normalized)
        if repeated:
            return self._save(
                actor,
                fulfillment_id,
                transcript,
                normalized,
                confidence,
                VoiceResult(
                    utterance_id=utterance_id,
                    transcript=transcript,
                    outcome="duplicate",
                    speech="",
                    interpreter="none",
                    results=[],
                ),
            )
        if confidence is not None and confidence < context.settings.stt_min_confidence:
            return self._save(
                actor,
                fulfillment_id,
                transcript,
                normalized,
                confidence,
                VoiceResult(
                    utterance_id=utterance_id,
                    transcript=transcript,
                    outcome="low_confidence",
                    speech=NOT_HEARD,
                    interpreter="none",
                    results=[],
                ),
            )
        order_context, interpreter_context = self._order_context(fulfillment_id)
        parsed = parse(transcript, order_context)
        result = self._run(
            actor,
            fulfillment_id,
            transcript,
            parsed,
            interpreter_context,
            utterance_id,
            credentials,
        )
        return self._save(actor, fulfillment_id, transcript, normalized, confidence, result)

    # Context for the parser.

    def _order_context(self, fulfillment_id: int | None) -> tuple[OrderContext, InterpreterContext]:
        if fulfillment_id is None:
            return OrderContext(lines=(), notes=()), InterpreterContext(products=(), notes=())
        with self.context.db.session() as session:
            fulfillment = get_fulfillment(session, fulfillment_id)
            lines = active_lines(fulfillment)
            keys = {key for line in lines if (key := item_key(line))}
            aliases: dict[str, list[str]] = {}
            if keys:
                for row in session.scalars(
                    select(ProductAlias).where(ProductAlias.item_key.in_(keys))
                ):
                    aliases.setdefault(row.item_key, []).append(row.alias)
            line_targets = tuple(
                Target.build(
                    line.id,
                    "line",
                    line.title,
                    [line.title, *aliases.get(item_key(line) or "", [])],
                )
                for line in lines
            )
            notes = [note for note in fulfillment.notes if note.kind == NoteKind.PACKING]
            note_targets = tuple(Target.for_note(note.id, note.text, note.text) for note in notes)
            clarification = None
            pending = fulfillment.clarification
            until = fulfillment.clarification_until
            if pending and until is not None and until > self.context.now():
                clarification = Clarification(
                    action=Action(pending["action"]),
                    candidate_ids=tuple(int(value) for value in pending["candidates"]),
                    quantity=pending.get("quantity"),
                    everything=bool(pending.get("everything")),
                )
            interpreter = InterpreterContext(
                products=tuple(
                    InterpreterProduct(line.id, line.title, line.required, line.confirmed)
                    for line in lines
                ),
                notes=tuple(
                    InterpreterNote(note.id, note.text, note.kind, note.confirmed_at is not None)
                    for note in fulfillment.notes
                ),
            )
            return OrderContext(line_targets, note_targets, clarification), interpreter

    # Running the commands.

    def _run(
        self,
        actor: Principal,
        fulfillment_id: int | None,
        transcript: str,
        parsed: ParseResult,
        interpreter_context: InterpreterContext,
        utterance_id: str,
        credentials: PrintCredentials,
    ) -> VoiceResult:
        def answer(
            outcome: Outcome,
            speech: str,
            results: list[ActionResult] | None = None,
            interpreter: Literal["rules", "ai", "none"] = "rules",
            navigated_to: int | None = None,
        ) -> VoiceResult:
            return VoiceResult(
                utterance_id=utterance_id,
                transcript=transcript,
                outcome=outcome,
                speech=speech,
                interpreter=interpreter,
                results=results or [],
                navigated_to=navigated_to,
            )

        if parsed.help:
            return answer("help", "אפשר לומר למשל: " + ", ".join(HelpText().examples) + ".")
        if parsed.clarification_cancelled:
            self._set_clarification(fulfillment_id, None)
            return answer("executed", "בסדר, לא שיניתי כלום.")
        if parsed.ignored:
            return answer("ignored", "", interpreter="none")
        commands = list(parsed.commands)
        interpreter: Literal["rules", "ai", "none"] = "rules"
        if any(command.unknown for command in commands) and fulfillment_id is not None:
            commands, used = self._ask_ai(transcript, commands, interpreter_context)
            interpreter = "ai" if used else "rules"
        results: list[ActionResult] = []
        current = fulfillment_id
        navigated_to = None
        clarified = False
        for index, command in enumerate(commands):
            if command.candidates and command.target_id is None:
                question = self._question(current, command)
                results.append(
                    ActionResult(
                        ok=False,
                        action=command.action,
                        code="clarify",
                        message=question,
                        speech=question,
                    )
                )
                clarified = True
                continue
            if command.unknown and command.target_id is None:
                speech = f"לא מצאתי בהזמנה את {command.unknown}."
                results.append(
                    ActionResult(
                        ok=False,
                        action=command.action,
                        code="line_not_found",
                        message=speech,
                        speech=speech,
                    )
                )
                continue
            if current is None and command.action not in (Action.NEXT_ORDER, Action.PREVIOUS_ORDER):
                speech = 'אין הזמנה פתוחה. אמרו "עבור להזמנה הבאה".'
                results.append(
                    ActionResult(
                        ok=False,
                        action=command.action,
                        code="not_claimed",
                        message=speech,
                        speech=speech,
                    )
                )
                continue
            request = ActionInput(
                action=command.action,
                line_id=command.target_id if command.target_kind == "line" else None,
                note_id=command.target_id if command.target_kind == "note" else None,
                quantity=command.quantity,
                everything=command.everything,
                reason=command.reason,
                print_mode=command.print_mode,
                source=ActionSource.VOICE,
                utterance_id=utterance_id,
                idempotency_key=f"voice:{utterance_id}:{index}",
            )
            try:
                result = self.work.act(actor, current or 0, request, credentials=credentials)
            except ServiceError as error:
                result = ActionResult(
                    ok=False,
                    action=command.action,
                    code=error.code,
                    message=error.message,
                    speech=error.message,
                )
            results.append(result)
            if result.ok and result.navigated_to is not None:
                current = result.navigated_to
                navigated_to = result.navigated_to
        if clarified:
            outcome: Outcome = "clarify"
        elif any(result.ok for result in results):
            outcome = "executed"
            if fulfillment_id is not None and current == fulfillment_id:
                self._set_clarification(fulfillment_id, None)
        else:
            outcome = "failed"
        speech = " ".join(result.speech for result in results if result.speech)
        return answer(outcome, speech, results, interpreter, navigated_to)

    def _question(self, fulfillment_id: int | None, command: Command) -> str:
        if fulfillment_id is None:
            return "לא הבנתי למה התכוונת."
        with self.context.db.session(write=True) as session:
            fulfillment = get_fulfillment(session, fulfillment_id, lock=True)
            names = {line.id: line.title for line in fulfillment.lines}
            names.update({note.id: note.text for note in fulfillment.notes})
            options = [names[item] for item in command.candidates if item in names][:4]
            question = "למה התכוונת: " + " או ".join(options) + "?"
            fulfillment.clarification = {
                "action": str(command.action),
                "candidates": list(command.candidates[:4]),
                "quantity": command.quantity,
                "everything": command.everything,
                "question": question,
            }
            fulfillment.clarification_until = self.context.now() + timedelta(
                seconds=CLARIFICATION_SECONDS
            )
            return question

    def _set_clarification(self, fulfillment_id: int | None, value: None) -> None:
        if fulfillment_id is None:
            return
        with self.context.db.session(write=True) as session:
            fulfillment = session.get(Fulfillment, fulfillment_id, with_for_update=True)
            if fulfillment is not None and fulfillment.clarification is not None:
                fulfillment.clarification = value
                fulfillment.clarification_until = None

    def _ask_ai(
        self, transcript: str, commands: list[Command], context: InterpreterContext
    ) -> tuple[list[Command], bool]:
        """Let the AI name the product the rules could not find; check what it says."""
        interpreter = self.context.providers.interpreter
        if interpreter is None:
            return commands, False
        try:
            interpretation = interpreter.interpret(transcript, context)
        except ProviderError as error:
            logger.warning("The AI interpreter failed", extra={"event": {"code": error.code}})
            return commands, False
        known = [command for command in commands if not command.unknown]
        if interpretation.unrelated:
            return known, True
        products = {product.id: product for product in context.products}
        notes = {note.id: note for note in context.notes}
        resolved: list[Command] = []
        for proposal in interpretation.actions[:5]:
            command = self._checked(proposal, products, notes)
            if command is not None and command not in known:
                resolved.append(command)
        if not resolved:
            return commands, True
        return [*known, *resolved], True

    def _checked(
        self,
        proposal: ProposedAction,
        products: dict[int, InterpreterProduct],
        notes: dict[int, InterpreterNote],
    ) -> Command | None:
        try:
            action = Action(proposal.action)
        except ValueError:
            return None
        if action not in AI_ACTIONS:
            return None
        if action is Action.CONFIRM_ORDER_NOTE:
            if proposal.note_id not in notes:
                return None
            return Command(action, target_id=proposal.note_id, target_kind="note")
        product = products.get(proposal.product_id or -1)
        if product is None:
            return None
        quantity = proposal.quantity
        if quantity is not None and not 1 <= quantity <= product.required:
            return None
        return Command(action, target_id=product.id, target_kind="line", quantity=quantity)

    # Recording.

    def _repeated(self, session: Session, actor: Principal, normalized: str) -> bool:
        window = self.context.settings.voice_duplicate_seconds
        if window <= 0 or not normalized:
            return False
        since = self.context.now() - timedelta(seconds=window)
        last = session.scalars(
            select(Utterance)
            .where(Utterance.actor == actor.id, Utterance.created_at >= since)
            .order_by(Utterance.created_at.desc())
            .limit(1)
        ).first()
        return last is not None and last.normalized == normalized and last.outcome == "executed"

    def _save(
        self,
        actor: Principal,
        fulfillment_id: int | None,
        transcript: str,
        normalized: str,
        confidence: float | None,
        result: VoiceResult,
    ) -> VoiceResult:
        stored = replace_fulfillment(result)
        with self.context.db.session(write=True) as session:
            if session.get(Utterance, result.utterance_id) is None:
                session.add(
                    Utterance(
                        id=result.utterance_id,
                        actor=actor.id,
                        fulfillment_id=fulfillment_id,
                        transcript=transcript,
                        normalized=normalized,
                        confidence=confidence,
                        outcome=result.outcome,
                        response=stored.model_dump(mode="json"),
                        created_at=self.context.now(),
                    )
                )
        return self._attach(stored, actor, fulfillment_id)

    def _attach(
        self, result: VoiceResult, actor: Principal, fulfillment_id: int | None = None
    ) -> VoiceResult:
        target = result.navigated_to or fulfillment_id
        if target is None:
            return result
        with self.context.db.session() as session:
            fulfillment = session.get(Fulfillment, target)
            if fulfillment is not None:
                result.fulfillment = self.presenter.fulfillment(session, fulfillment, actor)
        return result


def replace_fulfillment(result: VoiceResult) -> VoiceResult:
    """The stored answer, without the order views (they are rebuilt when replayed)."""
    copy = result.model_copy(deep=True)
    copy.fulfillment = None
    for item in copy.results:
        item.fulfillment = None
    return copy
