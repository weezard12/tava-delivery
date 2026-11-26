"""The speech client, workspace sign-in and the AI interpreter, without any network."""

import asyncio
import json
from typing import Any

import anthropic
import httpx
import httpx2
import pytest

from teva_delivery.domain.errors import ProviderError, ServiceError
from teva_delivery.domain.ports import InterpreterContext, InterpreterNote, InterpreterProduct
from teva_delivery.providers.interpreter import (
    ClaudeInterpreter,
    mask,
    parse_interpretation,
    request_content,
)
from teva_delivery.providers.speech import SpeechClient
from teva_delivery.providers.workspace_auth import auth_client, verify_session

CONTEXT = InterpreterContext(
    products=[InterpreterProduct(id=1, name="סרום ויטמין C כתום", required=1, confirmed=0)],
    notes=[
        InterpreterNote(
            id=5, text="להתקשר ל-050-1234567 או dana@example.com", kind="packing", confirmed=False
        ),
        InterpreterNote(id=6, text="כבר בוצע", kind="packing", confirmed=True),
    ],
)


def test_speech_sends_the_clip_and_reads_the_words() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/transcribe"
        assert request.url.params["language"] == "he"
        assert request.headers["content-type"] == "audio/webm"
        assert request.headers["authorization"] == "Bearer stt-token"
        return httpx.Response(200, json={"text": " שמתי סבון ", "confidence": 0.8, "duration": 1.2})

    client = SpeechClient(
        "http://stt:9000", token="stt-token", transport=httpx.MockTransport(handler)
    )
    transcript = client.transcribe(b"audio", "audio/webm;codecs=opus")
    assert (transcript.text, transcript.confidence) == ("שמתי סבון", 0.8)


@pytest.mark.parametrize(
    ("status", "code"), [(413, "audio_too_large"), (415, "audio_invalid"), (503, "stt_unavailable")]
)
def test_speech_failures(status: int, code: str) -> None:
    client = SpeechClient(
        "http://stt:9000", transport=httpx.MockTransport(lambda request: httpx.Response(status))
    )
    with pytest.raises(ProviderError) as error:
        client.transcribe(b"audio", "audio/ogg")
    assert error.value.code == code


def test_speech_refuses_other_media() -> None:
    client = SpeechClient("http://stt:9000")
    with pytest.raises(ProviderError) as error:
        client.transcribe(b"audio", "video/mp4")
    assert error.value.code == "audio_invalid"


def verify(handler: Any, token: str | None = "t" * 32) -> Any:
    async def run() -> Any:
        async with auth_client("http://auth", "k" * 40, httpx.MockTransport(handler)) as client:
            return await verify_session(
                client, token, method="POST", origin="https://teva.example", csrf_token="c"
            )

    return asyncio.run(run())


def test_sign_in_returns_the_approved_account() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/session/verify"
        assert request.headers["authorization"] == "Bearer " + "k" * 40
        assert json.loads(request.content)["method"] == "POST"
        return httpx.Response(200, json={"id": "dana", "role": "admin"})

    principal = verify(handler)
    assert (principal.id, principal.admin) == ("dana", True)


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {}, "invalid_session"),
        (403, {}, "forbidden"),
        (500, {}, "workspace_auth_unavailable"),
        (200, {"id": "", "role": "user"}, "workspace_auth_unavailable"),
    ],
)
def test_sign_in_failures(status: int, body: dict[str, Any], code: str) -> None:
    with pytest.raises(ServiceError) as error:
        verify(lambda request: httpx.Response(status, json=body))
    assert error.value.code == code


def test_an_implausible_cookie_is_refused_without_asking() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("no request expected")

    with pytest.raises(ServiceError):
        verify(handler, token="short")


def test_the_interpreter_never_sees_contact_details() -> None:
    content = request_content("שמתי את הכתום", CONTEXT)
    assert "050-1234567" not in content and "dana@example.com" not in content
    assert "כבר בוצע" not in content
    assert "שמתי את הכתום" in content
    assert mask("טלפון 0501234567") == "טלפון ###"


def test_the_interpretation_is_read_defensively() -> None:
    parsed = parse_interpretation(
        {
            "unrelated": False,
            "clarification": " ",
            "actions": [
                {"action": "confirm_product", "product_id": 1, "note_id": None, "quantity": 2},
                {"action": "complete_order", "product_id": None},
                {"action": "confirm_product", "product_id": True},
                "nonsense",
            ],
        }
    )
    assert [(a.action, a.product_id, a.quantity) for a in parsed.actions] == [
        ("confirm_product", 1, 2),
        ("confirm_product", None, None),
    ]
    assert parsed.clarification is None
    with pytest.raises(ProviderError):
        parse_interpretation(["not", "an", "object"])


def anthropic_client(handler: Any) -> anthropic.Anthropic:
    # The SDK has its own HTTP stack (httpx2); its mock transport works like httpx's.
    return anthropic.Anthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


def message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_the_interpreter_asks_for_structured_output_with_fallbacks() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
        answer = {
            "unrelated": False,
            "clarification": None,
            "actions": [
                {
                    "action": "confirm_product",
                    "product_id": 1,
                    "note_id": None,
                    "quantity": None,
                    "reason": None,
                }
            ],
        }
        return httpx2.Response(200, json=message(json.dumps(answer)))

    interpreter = ClaudeInterpreter("k", model="claude-opus-5-5", client=anthropic_client(handler))
    result = interpreter.interpret("שמתי את הכתום", CONTEXT)
    assert result.actions[0].product_id == 1
    body = sent[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "low"


def test_haiku_gets_no_fallbacks() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert "fallbacks" not in json.loads(request.content)
        return httpx2.Response(
            200, json=message('{"unrelated": true, "actions": [], "clarification": null}')
        )

    interpreter = ClaudeInterpreter("k", model="claude-haiku-5-5", client=anthropic_client(handler))
    assert interpreter.interpret("מה נשמע", CONTEXT).unrelated


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx2.Response(200, json=message("", "refusal")), "ai_refused"),
        (httpx2.Response(200, json=message("{", "max_tokens")), "ai_unexpected_response"),
        (httpx2.Response(200, json=message("not json")), "ai_unexpected_response"),
        (
            httpx2.Response(
                529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
            ),
            "ai_unavailable",
        ),
    ],
)
def test_interpreter_failures(response: httpx2.Response, code: str) -> None:
    interpreter = ClaudeInterpreter(
        "k", model="claude-opus-5-5", client=anthropic_client(lambda request: response)
    )
    with pytest.raises(ProviderError) as error:
        interpreter.interpret("שמתי", CONTEXT)
    assert error.value.code == code


def test_an_unreachable_interpreter() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    interpreter = ClaudeInterpreter("k", model="claude-opus-5-5", client=anthropic_client(handler))
    with pytest.raises(ProviderError) as error:
        interpreter.interpret("שמתי", CONTEXT)
    assert error.value.code == "ai_unavailable"
