"""Response extraction, diagnostics, and Azure error translation.

Covers both API surfaces: Chat Completions (what model-router supports, and
qq's default) and Responses (available on direct model deployments).
"""

import types

import openai
import pytest

from qq.client import (
    Answer,
    FoundryBackend,
    extract_chat_text,
    extract_responses_text,
    translate_error,
)
from qq.config import Settings
from qq.errors import AuthError, ConfigError, NetworkError, QQError


def settings(**kwargs):
    base = {"endpoint": "https://x.openai.azure.com", "api_key": "k", "deployment": "qq-router"}
    base.update(kwargs)
    return Settings(**base)


# --- Chat Completions shapes ------------------------------------------------


class ChatUsage:
    def __init__(self, prompt, completion):
        self.prompt_tokens = prompt
        self.completion_tokens = completion


def chat_response(text="hello", model="gpt-5.6-luna-2026-07-09", usage=None):
    message = types.SimpleNamespace(content=text)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice], model=model, usage=usage)


def chat_chunk(delta=None, model="gpt-5.6-luna-2026-07-09", usage=None):
    choices = []
    if delta is not None:
        choices = [types.SimpleNamespace(delta=types.SimpleNamespace(content=delta))]
    return types.SimpleNamespace(choices=choices, model=model, usage=usage)


# --- Responses shapes -------------------------------------------------------


class ResponsesUsage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class ResponsesResult:
    def __init__(self, text=None, model="gpt-5.6-sol", output=None, usage=None):
        if text is not None:
            self.output_text = text
        self.model = model
        self.output = output or []
        self.usage = usage


# --- extraction -------------------------------------------------------------


def test_chat_extraction_reads_the_message_content():
    assert extract_chat_text(chat_response(" hi there ")) == "hi there"


def test_chat_extraction_handles_plain_dictionaries():
    payload = {"choices": [{"message": {"content": "dict answer"}}]}
    assert extract_chat_text(payload) == "dict answer"


def test_chat_extraction_is_empty_when_there_is_no_content():
    assert extract_chat_text(types.SimpleNamespace(choices=[])) == ""


def test_responses_extraction_prefers_output_text():
    assert extract_responses_text(ResponsesResult(text="  hello  ")) == "hello"


def test_responses_extraction_walks_output_blocks_as_a_fallback():
    item = types.SimpleNamespace(
        content=[types.SimpleNamespace(text="from "), types.SimpleNamespace(text="blocks")]
    )
    assert extract_responses_text(ResponsesResult(output=[item])) == "from blocks"


def test_responses_extraction_handles_plain_dictionaries():
    payload = {"output": [{"content": [{"text": "dict answer"}]}]}
    assert extract_responses_text(payload) == "dict answer"


def test_responses_extraction_is_empty_when_there_is_nothing():
    assert extract_responses_text(ResponsesResult(text="   ")) == ""


# --- diagnostics ------------------------------------------------------------


def test_diagnostics_reports_model_deployment_and_latency():
    line = Answer(
        text="x", model="gpt-5.6-luna", deployment="qq-router", latency=0.713
    ).diagnostics()
    assert line == "[deployment=qq-router model=gpt-5.6-luna latency=0.71s]"


def test_diagnostics_includes_tokens_when_known():
    line = Answer(
        text="x",
        model="gpt-5.6-sol",
        deployment="qq-router",
        latency=1.0,
        input_tokens=12,
        output_tokens=148,
    ).diagnostics()
    assert "tokens=12in/148out" in line


def test_diagnostics_never_contains_the_api_key():
    secret = "super-secret-key-value"
    backend = FoundryBackend(settings(api_key=secret))
    line = Answer(text="x", model="m", deployment=backend.target, latency=0.1).diagnostics()
    assert secret not in line


# --- target and surface selection -------------------------------------------


def test_explicit_model_overrides_the_router_deployment():
    assert FoundryBackend(settings()).target == "qq-router"
    assert FoundryBackend(settings(model="gpt-5.6-sol")).target == "gpt-5.6-sol"


def test_default_surface_is_chat_because_the_router_requires_it():
    assert FoundryBackend(settings()).surface == "chat"
    assert FoundryBackend(settings(api="auto")).surface == "chat"


def test_responses_surface_is_opt_in():
    assert FoundryBackend(settings(api="responses")).surface == "responses"


# --- error translation ------------------------------------------------------


class _FakeHTTPResponse:
    """Minimal stand-in for the SDK's HTTP response.

    Built by hand rather than with httpx so the tests do not depend on which
    HTTP library the openai package happens to vendor.
    """

    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self.request = object()


def _status_error(cls, status, message="boom"):
    return cls(message, response=_FakeHTTPResponse(status), body=None)


@pytest.mark.parametrize(
    ("cls", "status", "expected"),
    [
        (openai.AuthenticationError, 401, AuthError),
        (openai.PermissionDeniedError, 403, AuthError),
        (openai.NotFoundError, 404, ConfigError),
        (openai.RateLimitError, 429, NetworkError),
        (openai.BadRequestError, 400, QQError),
    ],
)
def test_azure_errors_map_to_actionable_qq_errors(cls, status, expected):
    translated = translate_error(_status_error(cls, status))
    assert isinstance(translated, expected)
    assert translated.message


def test_connection_errors_become_network_errors():
    assert isinstance(translate_error(openai.APIConnectionError(request=object())), NetworkError)


def test_auth_failure_hint_mentions_both_auth_paths():
    translated = translate_error(_status_error(openai.AuthenticationError, 401))
    assert "az login" in translated.hint
    assert "QQ_API_KEY" in translated.hint


def test_unsupported_operation_points_at_the_api_surface():
    """The exact 400 Azure returns when Responses is used against the router."""
    translated = translate_error(
        _status_error(openai.BadRequestError, 400, "The requested operation is unsupported.")
    )
    assert translated.hint is not None
    assert "Chat Completions" in translated.hint


def test_key_auth_requires_a_key():
    backend = FoundryBackend(Settings(endpoint="https://x.openai.azure.com", auth="key"))
    with pytest.raises(ConfigError):
        backend._build_client()


# --- request round trips ----------------------------------------------------


def test_chat_call_sends_the_system_instruction_and_reports_the_routed_model():
    backend = FoundryBackend(settings())
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return chat_response("42", "gpt-5.6-terra", ChatUsage(11, 3))

    backend._cached = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions())
    )
    answer = backend.ask("what is 6x7")

    assert answer.text == "42"
    assert answer.model == "gpt-5.6-terra"
    assert answer.deployment == "qq-router"
    assert (answer.input_tokens, answer.output_tokens) == (11, 3)
    assert captured["model"] == "qq-router"
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user"]
    assert "terminal" in captured["messages"][0]["content"].lower()
    assert captured["messages"][1]["content"] == "what is 6x7"


def test_chat_streaming_collects_deltas_model_and_usage():
    backend = FoundryBackend(settings())
    chunks = [
        chat_chunk("hello "),
        chat_chunk("world"),
        chat_chunk(None, usage=ChatUsage(10, 11)),
    ]

    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            assert kwargs["stream_options"] == {"include_usage": True}
            return iter(chunks)

    backend._cached = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions())
    )
    seen = []
    answer = backend.ask("hi", stream=True, on_delta=seen.append)

    assert seen == ["hello ", "world"]
    assert answer.text == "hello world"
    assert answer.model == "gpt-5.6-luna-2026-07-09"
    assert (answer.input_tokens, answer.output_tokens) == (10, 11)


def test_responses_call_uses_the_instructions_parameter():
    backend = FoundryBackend(settings(api="responses", model="gpt-5.6-sol"))
    captured = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return ResponsesResult(text="42", model="gpt-5.6-sol", usage=ResponsesUsage(7, 2))

    backend._cached = types.SimpleNamespace(responses=FakeResponses())
    answer = backend.ask("what is 6x7")

    assert answer.text == "42"
    assert answer.model == "gpt-5.6-sol"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["input"] == "what is 6x7"
    assert "terminal" in captured["instructions"].lower()
    assert (answer.input_tokens, answer.output_tokens) == (7, 2)


def test_responses_streaming_collects_deltas_and_final_metadata():
    backend = FoundryBackend(settings(api="responses"))
    final = ResponsesResult(text="hello world", model="gpt-5.6-luna", usage=ResponsesUsage(5, 2))
    events = [
        types.SimpleNamespace(type="response.output_text.delta", delta="hello "),
        types.SimpleNamespace(type="response.output_text.delta", delta="world"),
        types.SimpleNamespace(type="response.completed", response=final),
    ]

    class FakeResponses:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return iter(events)

    backend._cached = types.SimpleNamespace(responses=FakeResponses())
    seen = []
    answer = backend.ask("hi", stream=True, on_delta=seen.append)

    assert seen == ["hello ", "world"]
    assert answer.text == "hello world"
    assert answer.model == "gpt-5.6-luna"


def test_empty_answers_are_rejected():
    backend = FoundryBackend(settings())

    class FakeCompletions:
        def create(self, **kwargs):
            return chat_response("")

    backend._cached = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions())
    )
    with pytest.raises(QQError):
        backend.ask("hello")
