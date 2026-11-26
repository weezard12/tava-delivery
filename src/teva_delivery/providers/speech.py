"""The local speech recognition service (``stt/``, faster-whisper), which hears Hebrew.

Audio never leaves the deployment: the packing screen records a short clip, this service
forwards it to the STT container on the internal network and gets the words back.
"""

import httpx

from ..domain.errors import ProviderError
from ..domain.ports import Transcript

TIMEOUT = httpx.Timeout(30.0, connect=3.0)
AUDIO_TYPES = frozenset({"audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mp4"})
MAX_TEXT = 1000


class SpeechClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers=headers,
        )

    def close(self) -> None:
        self._client.close()

    def transcribe(self, audio: bytes, content_type: str) -> Transcript:
        media = content_type.split(";", 1)[0].strip().lower()
        if media not in AUDIO_TYPES:
            raise ProviderError("audio_invalid", sent=False)
        try:
            response = self._client.post(
                "/v1/transcribe",
                content=audio,
                headers={"Content-Type": media},
                params={"language": "he"},
            )
        except httpx.HTTPError:
            raise ProviderError("stt_unavailable", retryable=True, sent=False) from None
        if response.status_code in (400, 415, 422):
            raise ProviderError("audio_invalid", sent=False)
        if response.status_code == 413:
            raise ProviderError("audio_too_large", sent=False)
        if response.status_code != 200:
            raise ProviderError("stt_unavailable", retryable=True, sent=False)
        try:
            data = response.json()
        except ValueError:
            raise ProviderError("stt_unavailable", sent=False) from None
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise ProviderError("stt_unavailable", sent=False)
        confidence = data.get("confidence")
        duration = data.get("duration")
        language = data.get("language")
        return Transcript(
            text=data["text"].strip()[:MAX_TEXT],
            confidence=(
                float(confidence)
                if isinstance(confidence, int | float) and 0 <= confidence <= 1
                else None
            ),
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
            language=language if isinstance(language, str) else None,
        )
