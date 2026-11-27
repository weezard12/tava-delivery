"""Local Hebrew speech recognition for the packing screen (faster-whisper).

One endpoint, ``POST /v1/transcribe``: the body is a short recording (webm, ogg, wav or mp4),
the answer is ``{"text", "confidence", "duration", "language"}``. Audio is decoded in memory
and never written to disk or logged. Only the warehouse service calls it, on the internal
network, with ``STT_TOKEN`` as a bearer token when one is set.
"""

import hmac
import io
import logging
import math
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, Request
from faster_whisper import WhisperModel

MODEL = os.environ.get("STT_MODEL", "ivrit-ai/whisper-large-v3-turbo-ct2")
DEVICE = os.environ.get("STT_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("STT_COMPUTE_TYPE", "int8")
TOKEN = os.environ.get("STT_TOKEN", "")
MAX_BYTES = 2_000_000
MAX_SECONDS = 20.0
TYPES = frozenset({"audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mp4"})
# Words the packing screen hears often. They nudge recognition, they never force it.
PROMPT = "שמתי, חסר, מדבקה, הזמנה הבאה, תבטל, הערות, סרום, סבון, שמן, קרם"

logger = logging.getLogger("teva_stt")
_model: WhisperModel | None = None
_lock = threading.Lock()


def model() -> WhisperModel:
    global _model
    with _lock:
        if _model is None:
            logger.warning("Loading speech model %s on %s", MODEL, DEVICE)
            _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)
        return _model


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # The first start downloads the model; health stays 503 until it is loaded.
    threading.Thread(target=model, daemon=True).start()
    yield


app = FastAPI(title="Teva STT", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, str]:
    if _model is None:
        raise HTTPException(503, "model_loading")
    return {"status": "ok"}


@app.post("/v1/transcribe")
async def transcribe(
    request: Request,
    language: Annotated[str, Query(pattern="^[a-z]{2}$")] = "he",
    authorization: Annotated[str | None, Header()] = None,
    content_type: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    if TOKEN and not hmac.compare_digest(authorization or "", f"Bearer {TOKEN}"):
        raise HTTPException(401, "invalid_token")
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media not in TYPES:
        raise HTTPException(415, "audio_invalid")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "audio_invalid")
    if len(audio) > MAX_BYTES:
        raise HTTPException(413, "audio_too_large")
    try:
        segments, info = model().transcribe(
            io.BytesIO(audio),
            language=language,
            beam_size=5,
            vad_filter=True,
            initial_prompt=PROMPT,
            condition_on_previous_text=False,
        )
        parts = [segment for segment in segments if segment.start < MAX_SECONDS]
    except Exception as error:  # noqa: BLE001 - the audio decoder raises many types
        # The decoder's message may quote the input; only the type is logged.
        logger.warning("Could not decode a recording (%s)", type(error).__name__)
        raise HTTPException(422, "audio_invalid") from None
    text = " ".join(segment.text.strip() for segment in parts).strip()
    confidence = (
        sum(math.exp(segment.avg_logprob) for segment in parts) / len(parts) if parts else 0.0
    )
    return {
        "text": text,
        "confidence": round(min(max(confidence, 0.0), 1.0), 3),
        "duration": round(info.duration, 2),
        "language": info.language,
    }
