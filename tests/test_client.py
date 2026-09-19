"""Response extraction, diagnostics, and Azure error translation.

Covers both API surfaces: Chat Completions (model-router on an account
endpoint) and Responses (project endpoints, OpenRouter, and the search tool
loop that only Responses supports).
"""

import io
import json
import types

import openai
import pytest

from qq.azure import AzureFoundryBackend as FoundryBackend
from qq.client import (
    Answer,
    FailoverBackend,
    build_backend,
    extract_chat_text,
    extract_responses_text,
    translate_error,
)
from qq.config import Settings
from qq.errors import AuthError, ConfigError, NetworkError, QQError, RateLimitedError
from qq.search import TOOL_DEFINITION, WebSearch


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


def test_default_surface_is_chat_on_the_account_endpoint():
    """model-router rejects Responses there, so auto has to mean chat."""
    assert FoundryBackend(settings()).surface == "chat"
    assert FoundryBackend(settings(api="auto")).surface == "chat"


def test_default_surface_is_responses_on_a_project_endpoint():
    project = "https://x.services.ai.azure.com/api/projects/qq-dev"
    assert FoundryBackend(settings(endpoint=project)).surface == "responses"
    assert FoundryBackend(settings(endpoint=project, api="chat")).surface == "chat"


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


def _stream_error(message, code=None):
    """What the SDK raises when an SSE error event interrupts a good response.

    No status code: the request itself returned 200 and failed afterwards.
    """
    body = {"message": message}
    if code is not None:
        body["code"] = code
    return openai.APIError(message, request=object(), body=body)


def test_a_rate_limit_that_arrives_mid_stream_is_still_a_rate_limit():
    """qq streams to a terminal, so this is the path a busy router takes.

    The SDK raises a bare APIError for an error event inside the stream, which
    has no status code to match on; without this the user got "OpenAI client
    error" and exit 1 for what is a wait-and-retry.
    """
    translated = translate_error(
        _stream_error("Model deployment rate limit exceeded. The system is busy.", code="429")
    )
    assert isinstance(translated, NetworkError)
    assert translated.message == "rate limited by Azure (429)"
    assert "routerCapacity" in translated.hint


def test_a_rate_limit_mid_stream_is_caught_without_a_code():
    translated = translate_error(_stream_error("Requests to the model have hit a rate limit."))
    assert isinstance(translated, NetworkError)


def test_other_mid_stream_failures_name_the_provider():
    translated = translate_error(_stream_error("upstream connection reset"))
    assert isinstance(translated, QQError)
    assert translated.message == "Azure broke off mid-answer: upstream connection reset"


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


# --- tiered diagnostics -----------------------------------------------------


def full_answer(**overrides):
    base = {
        "text": "x",
        "provider": "azure",
        "model": "gpt-5.6-luna-2026-07-09",
        "deployment": "qq-router",
        "latency": 2.63,
        "input_tokens": 197,
        "output_tokens": 108,
        "router": "model-router:2025-11-18",
        "host": "qq-dev-abc.openai.azure.com",
        "api": "chat",
        "auth": "entra",
        "stream": False,
        "request_id": "chatcmpl-ABC123",
        "server_timings": {
            "pre_inference_ms": 39,
            "engine_ttft_ms": 99,
            "engine_ttlt_ms": 183,
            "service_ttft_ms": 334,
            "service_ttlt_ms": 431,
            "user_visible_ttft_ms": 294,
        },
        "replica": "gpt56-l-usc-gb3-oai-oe-5b5xdp",
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "token_cache": "hit",
        "tenant": "00000000-1111-2222-3333-444444444444",
    }
    base.update(overrides)
    return Answer(**base)


def test_level_one_names_the_provider_first():
    """With more than one backend, -v has to say which one answered."""
    expected = (
        "[provider=azure deployment=qq-router model=gpt-5.6-luna-2026-07-09 "
        "latency=2.63s tokens=197in/108out]"
    )
    assert full_answer().diagnostics(1) == expected
    assert full_answer().diagnostics() == expected


def test_level_one_distinguishes_the_two_backends():
    azure = full_answer().diagnostics(1)
    openrouter = full_answer(
        provider="openrouter",
        deployment="openrouter/auto",
        model="deepseek/deepseek-v4-flash-0731",
    ).diagnostics(1)
    assert "provider=azure" in azure
    assert "provider=openrouter" in openrouter
    assert azure != openrouter


def test_provider_is_not_repeated_at_higher_tiers():
    assert "provider=" not in full_answer().diagnostics(2).splitlines()[1]


def test_tiers_are_additive_so_lower_lines_never_move():
    answer = full_answer()
    one = answer.diagnostics(1).splitlines()
    two = answer.diagnostics(2).splitlines()
    three = answer.diagnostics(3).splitlines()
    assert two[: len(one)] == one
    assert three[: len(two)] == two
    assert len(two) == 2
    assert len(three) == 4


def test_level_two_reports_connection_and_request_context():
    line = full_answer().diagnostics(2).splitlines()[1]
    assert "router=model-router:2025-11-18" in line
    assert "host=qq-dev-abc.openai.azure.com" in line
    assert "api=chat" in line
    assert "auth=entra" in line
    assert "stream=off" in line
    assert "request=chatcmpl-ABC123" in line


def test_level_two_marks_an_unknown_router_rather_than_guessing():
    line = full_answer(router=None).diagnostics(2).splitlines()[1]
    assert "router=?" in line


def test_level_three_reports_server_timings_in_reading_order():
    line = full_answer().diagnostics(3).splitlines()[2]
    assert line.startswith("[server ")
    assert line.index("pre_inference=39ms") < line.index("engine_ttft=99ms")
    assert line.index("engine_ttft=99ms") < line.index("service_ttft=334ms")
    assert "visible_ttft=294ms" in line


def test_level_three_reports_replica_and_token_detail():
    line = full_answer().diagnostics(3).splitlines()[3]
    assert "replica=gpt56-l-usc-gb3-oai-oe-5b5xdp" in line
    assert "cached=0" in line
    assert "reasoning=0" in line
    assert "token_cache=hit" in line


def test_client_overhead_separates_local_cost_from_service_cost():
    answer = full_answer()
    assert answer.client_overhead == pytest.approx(2.63 - 0.431, abs=0.001)
    assert "overhead=2.20s" in answer.diagnostics(3)


def test_overhead_is_omitted_when_the_service_reported_no_total():
    answer = full_answer(server_timings={})
    assert answer.client_overhead is None
    assert "overhead=" not in answer.diagnostics(3)


def test_missing_vendor_extensions_drop_lines_instead_of_printing_blanks():
    """Azure's routing and latency_checkpoint fields are undocumented extras."""
    bare = full_answer(
        server_timings={},
        replica=None,
        cached_tokens=None,
        reasoning_tokens=None,
        token_cache=None,
        tenant=None,
    )
    assert bare.diagnostics(3).splitlines() == bare.diagnostics(2).splitlines()
    assert "[server" not in bare.diagnostics(3)
    assert "[detail" not in bare.diagnostics(3)


def test_level_zero_renders_nothing():
    assert full_answer().diagnostics(0) == ""


def test_no_tier_leaks_the_api_key():
    secret = "azure-key-must-not-appear"
    backend = FoundryBackend(settings(api_key=secret, api="chat"))
    answer = full_answer(auth=backend.settings.effective_auth)
    for level in (1, 2, 3):
        assert secret not in answer.diagnostics(level)


def test_backend_collects_azure_vendor_extensions():
    """-vvv is only worth having if the extras actually reach the Answer."""
    backend = FoundryBackend(settings(tenant="tenant-1", router="model-router:2025-11-18"))

    usage = ChatUsage(11, 3)
    usage.model_extra = {"latency_checkpoint": {"service_ttlt_ms": 431, "engine_ttft_ms": 99}}
    usage.prompt_tokens_details = types.SimpleNamespace(cached_tokens=7)
    usage.completion_tokens_details = types.SimpleNamespace(reasoning_tokens=5)

    response = chat_response("42", "gpt-5.6-terra", usage)
    response.id = "chatcmpl-XYZ"
    response.model_extra = {"routing": {"serving_endpoint": "replica-7"}}

    class FakeCompletions:
        def create(self, **kwargs):
            return response

    backend._cached = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions())
    )
    answer = backend.ask("hi")

    assert answer.request_id == "chatcmpl-XYZ"
    assert answer.replica == "replica-7"
    assert answer.server_timings["service_ttlt_ms"] == 431
    assert answer.cached_tokens == 7
    assert answer.reasoning_tokens == 5
    assert answer.router == "model-router:2025-11-18"
    assert answer.tenant == "tenant-1"
    assert answer.api == "chat"


# --- the search tool loop ----------------------------------------------------


BRAVE_PAYLOAD = {
    "web": {
        "results": [
            {"title": "Python 3.14.7", "url": "https://python.org/r", "description": "Released."}
        ]
    }
}


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def brave_opener(payload=BRAVE_PAYLOAD):
    def opener(request, timeout=None):
        return _Body(json.dumps(payload).encode())

    return opener


def failing_opener(exc):
    def opener(request, timeout=None):
        raise exc

    return opener


def web(**kwargs):
    kwargs.setdefault("opener", brave_opener())
    return WebSearch("brave-key", **kwargs)


def function_call(call_id, query):
    return types.SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name="brave_search",
        arguments=json.dumps({"query": query}),
    )


def tool_round(*calls, model="gpt-5.6-luna", usage=(10, 5)):
    """A Responses result that asks for tool calls instead of answering."""
    return ResponsesResult(model=model, output=list(calls), usage=ResponsesUsage(*usage))


def answer_round(text, model="gpt-5.6-luna", usage=(20, 7), extra=()):
    return ResponsesResult(text=text, model=model, output=list(extra), usage=ResponsesUsage(*usage))


class ScriptedResponses:
    """Answers each create() from a script and records every request."""

    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        result = self.results.pop(0)
        return iter(result) if kwargs.get("stream") else result


def responses_backend(results, **overrides):
    overrides.setdefault("endpoint", "https://x.services.ai.azure.com/api/projects/p")
    backend = FoundryBackend(settings(**overrides))
    script = ScriptedResponses(results)
    backend._cached = types.SimpleNamespace(responses=script)
    return backend, script


def test_search_loop_runs_the_tool_and_asks_again():
    backend, script = responses_backend(
        [
            tool_round(function_call("c1", "newest python release")),
            answer_round("3.14.7\n\nSources: https://python.org/r"),
        ]
    )
    search = web()
    answer = backend.ask("newest python", search=search)

    assert answer.text == "3.14.7\n\nSources: https://python.org/r"
    assert [c.query for c in answer.searches] == ["newest python release"]
    assert answer.searches[0].hits == 1
    assert answer.search_enabled is True
    # Tokens are summed across rounds: the user paid for both.
    assert (answer.input_tokens, answer.output_tokens) == (30, 12)

    first, second = script.requests
    assert first["tools"] == [TOOL_DEFINITION]
    assert "tool_choice" not in first
    assert first["input"] == "newest python"
    assert "brave_search" in first["instructions"]
    assert second["input"][0] == {"role": "user", "content": "newest python"}
    assert second["input"][1]["type"] == "function_call"
    assert second["input"][1]["call_id"] == "c1"
    assert second["input"][2]["type"] == "function_call_output"
    assert second["input"][2]["call_id"] == "c1"
    assert "[https://python.org/r]" in second["input"][2]["output"]


def test_search_loop_forces_an_answer_after_the_round_limit():
    """A model that keeps refining its query has the tools taken away.

    Withheld, not disabled with ``tool_choice="none"``: the Azure router
    answers that with another function_call and no text at all.
    """
    backend, script = responses_backend(
        [
            tool_round(function_call("c1", "first try")),
            # Still asking for a search on the forced round; it must be ignored.
            answer_round("best guess", extra=[function_call("c2", "second try")]),
        ]
    )
    search = web(max_rounds=1)
    answer = backend.ask("q", search=search)

    assert answer.text == "best guess"
    assert len(script.requests) == 2
    assert "tools" not in script.requests[1]
    assert "tool_choice" not in script.requests[1]
    assert [c.query for c in search.calls] == ["first try"]


def test_an_empty_answer_carries_the_routing_details():
    """The failure the hint points --verbose at has something to print.

    A model that asks for a search on every round, the forced one included,
    leaves qq with no text. That is a failure, but not a mystery: the request
    still knows its deployment, its surface and what it searched for.
    """
    backend, script = responses_backend(
        [
            tool_round(function_call("c1", "first try")),
            tool_round(function_call("c2", "second try")),
        ]
    )
    with pytest.raises(QQError) as excinfo:
        backend.ask("q", search=web(max_rounds=1))

    assert "empty answer" in excinfo.value.message
    assert len(script.requests) == 2
    detail = excinfo.value.diagnostics(2)
    assert "deployment=qq-router" in detail
    assert "api=responses" in detail
    assert '"first try"' in detail
    assert excinfo.value.diagnostics(0) == ""


def test_a_transport_failure_carries_the_routing_details():
    """Same for an error raised by the SDK rather than by an empty body."""
    backend, _script = responses_backend([])
    backend._cached = types.SimpleNamespace(
        responses=types.SimpleNamespace(create=_raise(RuntimeError("boom")))
    )
    with pytest.raises(QQError) as excinfo:
        backend.ask("q")

    assert "deployment=qq-router" in excinfo.value.diagnostics(1)


def _raise(exc):
    def create(**kwargs):
        raise exc

    return create


def test_search_loop_runs_every_call_in_a_round():
    backend, script = responses_backend(
        [
            tool_round(function_call("c1", "a"), function_call("c2", "b")),
            answer_round("done"),
        ]
    )
    answer = backend.ask("q", search=web())

    assert [c.query for c in answer.searches] == ["a", "b"]
    items = script.requests[1]["input"]
    assert [i.get("type") for i in items[1:]] == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
    ]
    assert "search=2" in answer.diagnostics(1)


def test_search_loop_streams_only_the_answer():
    """A tool-call round has no text deltas; nothing prints until the answer."""
    final_call = tool_round(function_call("c1", "x"))
    final_answer = answer_round("3.14.7")
    backend, script = responses_backend(
        [
            [types.SimpleNamespace(type="response.completed", response=final_call)],
            [
                types.SimpleNamespace(type="response.output_text.delta", delta="3.14"),
                types.SimpleNamespace(type="response.output_text.delta", delta=".7"),
                types.SimpleNamespace(type="response.completed", response=final_answer),
            ],
        ]
    )
    seen = []
    answer = backend.ask("q", stream=True, on_delta=seen.append, search=web())

    assert seen == ["3.14", ".7"]
    assert answer.text == "3.14.7"
    assert len(answer.searches) == 1
    assert all(r["stream"] is True for r in script.requests)


def test_no_tool_is_offered_when_search_is_off():
    backend, script = responses_backend([answer_round("42")])
    answer = backend.ask("q")

    assert "tools" not in script.requests[0]
    assert "brave_search" not in script.requests[0]["instructions"]
    assert answer.search_enabled is False
    assert "search=" not in answer.diagnostics(1)


def test_search_needs_the_responses_surface():
    backend = FoundryBackend(settings(api="chat"))
    with pytest.raises(ConfigError):
        backend.ask("q", search=web())


def test_a_failed_search_still_yields_an_answer():
    backend, _ = responses_backend(
        [tool_round(function_call("c1", "x")), answer_round("from memory")]
    )
    import urllib.error

    broken = web(opener=failing_opener(urllib.error.URLError("offline")))
    answer = backend.ask("q", search=broken)

    assert answer.text == "from memory"
    assert answer.searches[0].error
    assert "search_error=" in answer.diagnostics(2)


def test_search_diagnostics_show_the_query_at_level_two_only():
    answer = full_answer(search_enabled=True)
    answer.searches = [types.SimpleNamespace(query="python release date", latency=0.8, error=None)]
    one, two = answer.diagnostics(2).splitlines()
    assert one.endswith("search=1]")
    assert 'search_query="python release date"' in two
    assert "search_latency=0.80s" in two
    assert "search_query" not in one


def test_responses_extraction_ignores_reasoning_and_tool_items():
    """Seen live on OpenRouter: a tool-call round returns a reasoning item and
    no message, and the fallback walk was printing the reasoning as the answer."""
    reasoning = types.SimpleNamespace(
        type="reasoning",
        content=[types.SimpleNamespace(type="reasoning_text", text="Let me think...")],
    )
    call = function_call("c1", "x")
    message = types.SimpleNamespace(
        type="message",
        content=[
            types.SimpleNamespace(type="output_text", text="the answer"),
            types.SimpleNamespace(type="refusal", text="nope"),
        ],
    )
    assert extract_responses_text(ResponsesResult(output=[reasoning, call])) == ""
    assert (
        extract_responses_text(ResponsesResult(output=[reasoning, call, message])) == "the answer"
    )


def test_search_loop_does_not_print_reasoning_from_tool_rounds():
    reasoning = types.SimpleNamespace(
        type="reasoning",
        content=[types.SimpleNamespace(type="reasoning_text", text="Need to search.")],
    )
    backend, _ = responses_backend(
        [
            ResponsesResult(
                model="m", output=[reasoning, function_call("c1", "q")], usage=ResponsesUsage(1, 1)
            ),
            answer_round("3.14.7"),
        ]
    )
    answer = backend.ask("q", search=web())
    assert answer.text == "3.14.7"


# --- the standby ------------------------------------------------------------


class _Fake:
    """A backend that answers, or fails in a scripted way."""

    provider_label = "The standby"

    def __init__(self, provider, text=None, raises=None, deltas=()):
        self.provider = provider
        self.text = text
        self.raises = raises
        self.deltas = deltas
        self.asked = []

    def ask(self, prompt, *, stream=False, on_delta=None, search=None):
        self.asked.append(prompt)
        for piece in self.deltas:
            if on_delta:
                on_delta(piece)
        if self.raises is not None:
            raise self.raises
        return Answer(text=self.text, provider=self.provider, model=f"{self.provider}-model")


def limited():
    return RateLimitedError("rate limited by Azure (429)", hint="Wait a moment.")


def test_a_rate_limited_primary_is_answered_by_the_standby():
    primary = _Fake("azure", raises=limited())
    standby = _Fake("openrouter", text="42")
    answer = FailoverBackend(primary, standby).ask("q")

    assert answer.text == "42"
    assert answer.provider == "openrouter"
    assert standby.asked == ["q"]
    # Invisible on stdout, so -v is where it has to show.
    assert "fallback=azure:429" in answer.diagnostics(1)
    assert "provider=openrouter" in answer.diagnostics(1)


def test_only_a_rate_limit_falls_over():
    """A 401 is a fact about the configuration; hiding it would be worse."""
    primary = _Fake("azure", raises=AuthError("Azure rejected the credentials (401)"))
    standby = _Fake("openrouter", text="42")
    with pytest.raises(AuthError):
        FailoverBackend(primary, standby).ask("q")
    assert standby.asked == []


def test_no_failover_once_the_answer_has_started_printing():
    """Two models spliced into one streamed answer would be worse than an error."""
    printed = []
    primary = _Fake("azure", raises=limited(), deltas=("half an ",))
    standby = _Fake("openrouter", text="whole answer")
    with pytest.raises(RateLimitedError):
        FailoverBackend(primary, standby).ask("q", stream=True, on_delta=printed.append)

    assert printed == ["half an "]
    assert standby.asked == []


def test_a_standby_that_also_fails_reports_the_original_rate_limit():
    primary = _Fake("azure", raises=limited())
    standby = _Fake("openrouter", raises=QQError("insufficient credits (402)"))
    with pytest.raises(RateLimitedError) as excinfo:
        FailoverBackend(primary, standby).ask("q")

    assert excinfo.value.message == "rate limited by Azure (429)"
    assert "insufficient credits (402)" in excinfo.value.hint


def test_a_healthy_primary_never_wakes_the_standby():
    primary = _Fake("azure", text="42")
    standby = _Fake("openrouter", text="43")
    answer = FailoverBackend(primary, standby).ask("q")

    assert answer.text == "42"
    assert standby.asked == []
    assert answer.fallback_from is None
    assert "fallback" not in answer.diagnostics(3)


def test_build_backend_without_a_standby_returns_the_bare_backend():
    settings_azure = settings(endpoint="https://x.services.ai.azure.com/api/projects/p")
    assert isinstance(build_backend(settings_azure), FoundryBackend)
    paired = build_backend(settings_azure, settings(provider="openrouter", api_key="or-key"))
    assert isinstance(paired, FailoverBackend)
    assert paired.provider == "azure"
    assert paired.standby.provider == "openrouter"
