"""Brave search tool: result shaping, error mapping, and the tool contract.

Nothing here touches the network. The HTTP opener is injected.
"""

import io
import json
import urllib.error

import pytest

from qq import __version__
from qq.errors import AuthError, ConfigError, NetworkError
from qq.search import (
    MAX_OUTPUT_CHARS,
    TOOL_DEFINITION,
    TOOL_NAME,
    SearchHit,
    WebSearch,
    brave_search,
    clean_snippet,
    format_results,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def opener_returning(payload):
    """An opener that records the request and answers with a JSON body."""
    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["timeout"] = timeout
        return FakeResponse(json.dumps(payload).encode())

    opener.seen = seen
    return opener


def opener_raising(exc):
    def opener(request, timeout=None):
        raise exc

    return opener


def http_error(code):
    return urllib.error.HTTPError("https://api.search.brave.com/x", code, "boom", {}, None)


BRAVE_PAYLOAD = {
    "web": {
        "results": [
            {
                "title": "What is a <strong>CNAME</strong>?",
                "url": "https://example.com/cname",
                "description": (
                    "A &quot;canonical name&quot; record <strong>aliases</strong> one name to another."
                ),
            },
            {"title": "Second", "url": "https://example.org/2", "description": "Plain."},
            {"title": "No url", "description": "dropped"},
        ]
    }
}


# --- the HTTP call ------------------------------------------------------------


def test_snippet_cleaning_strips_brave_markup_and_entities():
    assert (
        clean_snippet("A &quot;canonical&quot; <strong>name</strong>  x") == 'A "canonical" name x'
    )
    assert clean_snippet("") == ""


def test_search_sends_the_key_as_a_header_not_in_the_url():
    opener = opener_returning(BRAVE_PAYLOAD)
    brave_search("what is a CNAME", "brave-secret", opener=opener)
    assert opener.seen["headers"]["x-subscription-token"] == "brave-secret"
    assert "brave-secret" not in opener.seen["url"]
    assert "q=what+is+a+CNAME" in opener.seen["url"]
    assert "count=5" in opener.seen["url"]


def test_search_identifies_itself_and_its_version_to_brave():
    """qq is a named client of someone else's API, so it says which release.

    product/version is the conventional User-Agent form, and the version is the
    part that lets an operator tell one release of qq from another.
    """
    opener = opener_returning(BRAVE_PAYLOAD)
    brave_search("what is a CNAME", "k", opener=opener)
    assert (
        opener.seen["headers"]["user-agent"]
        == f"qq/{__version__} (+https://github.com/CrankingAI/qq-router)"
    )


def test_search_returns_cleaned_hits_and_drops_entries_without_a_url():
    hits = brave_search("q", "k", opener=opener_returning(BRAVE_PAYLOAD))
    assert [h.url for h in hits] == ["https://example.com/cname", "https://example.org/2"]
    assert hits[0].title == "What is a CNAME?"
    assert hits[0].snippet == 'A "canonical name" record aliases one name to another.'


def test_search_tolerates_an_empty_result_set():
    assert brave_search("q", "k", opener=opener_returning({"web": {}})) == []
    assert brave_search("q", "k", opener=opener_returning({})) == []


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_key_is_an_auth_error_with_a_hint(code):
    with pytest.raises(AuthError) as excinfo:
        brave_search("q", "bad", opener=opener_raising(http_error(code)))
    assert "BRAVE_API_KEY" in excinfo.value.hint


@pytest.mark.parametrize(
    "exc",
    [http_error(429), http_error(503), urllib.error.URLError("dns"), TimeoutError("slow")],
)
def test_rate_limits_and_outages_are_network_errors(exc):
    with pytest.raises(NetworkError):
        brave_search("q", "k", opener=opener_raising(exc))


# --- what the model reads -----------------------------------------------------


def test_format_puts_the_url_first_so_the_model_can_cite_it():
    text = format_results([SearchHit("T", "https://x/y", "snippet")])
    assert text.startswith("[https://x/y] T\nsnippet")


def test_format_caps_the_size_handed_to_the_model():
    hits = [SearchHit("T", "https://x/", "s" * 5000) for _ in range(5)]
    assert len(format_results(hits)) <= MAX_OUTPUT_CHARS


def test_format_says_so_when_there_is_nothing():
    assert format_results([]) == "No results."


# --- the tool as the backend sees it ------------------------------------------


def test_tool_definition_is_a_strict_function_with_one_string_argument():
    assert TOOL_DEFINITION["type"] == "function"
    assert TOOL_DEFINITION["name"] == TOOL_NAME == "brave_search"
    assert TOOL_DEFINITION["strict"] is True
    params = TOOL_DEFINITION["parameters"]
    assert params["required"] == ["query"]
    assert params["additionalProperties"] is False


def test_web_search_requires_a_key_up_front():
    with pytest.raises(ConfigError) as excinfo:
        WebSearch(None)
    assert "brave_api_key" in excinfo.value.hint


def test_run_parses_the_model_arguments_and_records_the_call():
    web = WebSearch("k", opener=opener_returning(BRAVE_PAYLOAD))
    output = web.run(json.dumps({"query": "cname record"}))
    assert "[https://example.com/cname]" in output
    assert len(web.calls) == 1
    assert web.calls[0].query == "cname record"
    assert web.calls[0].hits == 2
    assert web.calls[0].error is None


def test_run_hands_transient_failures_back_to_the_model_as_text():
    """The user still gets an answer; the model is told the search failed."""
    web = WebSearch("k", opener=opener_raising(http_error(503)))
    output = web.run('{"query": "x"}')
    assert output.startswith("Search failed")
    assert web.calls[0].error
    assert web.calls[0].hits == 0


def test_run_raises_on_a_rejected_key():
    web = WebSearch("bad", opener=opener_raising(http_error(401)))
    with pytest.raises(AuthError):
        web.run('{"query": "x"}')


@pytest.mark.parametrize("arguments", ["", "not json", "[]", '{"query": ""}', '{"other": 1}'])
def test_run_survives_malformed_arguments(arguments):
    web = WebSearch("k", opener=opener_returning(BRAVE_PAYLOAD))
    assert web.run(arguments).startswith("Search failed: empty query")
    assert web.calls[0].error == "empty query"
