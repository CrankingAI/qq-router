"""Web search for ``--search``: Brave Search, offered to the model as a tool.

qq does not decide when to search. The model does. The Responses API lets qq
offer ``brave_search`` as a function tool; the model calls it only when the
answer depends on something that may have changed since its training data, and
writes its own query, which is usually better than the words the user typed.
"how do I list my github repos" never triggers a search, so it costs nothing
extra; "newest stable Python release" does, and comes back with a source.

Two deliberate limits:

* At most ``MAX_SEARCH_ROUNDS`` rounds of searching per question, after which
  the model is told to answer with what it has. Without this, a model that is
  not finding what it wants can keep refining its query indefinitely.
* Results are trimmed to titles, URLs and snippets, and capped in size. The
  model gets enough to answer and cite, not enough to run up the input bill.

Search results are untrusted text from the open web. They go to the model as
tool output, which models treat as data rather than instructions, but that is a
convention rather than a guarantee. See SECURITY.md.

Only the standard library is used for the HTTP call. qq's startup time is
dominated by imports, and one small GET does not justify another dependency.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .errors import AuthError, ConfigError, NetworkError

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

#: The name the model sees. prompt.py refers to it in the system instruction,
#: so the two must agree.
TOOL_NAME = "brave_search"

#: Results per query. Five is enough to answer and cite; more mostly adds
#: input tokens.
DEFAULT_COUNT = 5

#: Rounds of searching the model may do before it is told to answer.
MAX_SEARCH_ROUNDS = 2

#: Ceiling on the text handed back per search, in characters.
MAX_OUTPUT_CHARS = 6000

#: Brave's own limit on the query string.
MAX_QUERY_CHARS = 400

KEY_HINT = (
    "Set QQ_BRAVE_API_KEY or BRAVE_API_KEY, or run 'qq config set brave_api_key <key>'. "
    "Keys: https://api-dashboard.search.brave.com/app/keys."
)

#: Function tool definition in the Responses API shape. ``strict`` makes the
#: model emit exactly ``{"query": ...}`` and nothing else.
TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "name": TOOL_NAME,
    "description": (
        "Search the web with Brave. Use only when the answer depends on facts that may "
        "have changed since your training data, or that you are unsure of. Returns "
        "titles, URLs and snippets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A concise web search query, not the user's question verbatim.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": True,
}

_TAG = re.compile(r"<[^>]+>")


def clean_snippet(text: str) -> str:
    """Strip the ``<strong>`` markup and entities Brave puts in snippets."""
    return " ".join(_TAG.sub("", html.unescape(text or "")).split())


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass
class SearchCall:
    """One executed search, kept for the diagnostics tiers."""

    query: str
    hits: int = 0
    latency: float = 0.0
    error: str | None = None


def format_results(hits: list[SearchHit], limit: int = MAX_OUTPUT_CHARS) -> str:
    """Render hits as the tool output the model reads.

    One block per hit, URL first so the model can cite it, then the title and
    snippet. Plain text rather than JSON: fewer tokens, nothing to mis-parse.
    """
    if not hits:
        return "No results."
    text = "\n\n".join(f"[{h.url}] {h.title}\n{h.snippet}".strip() for h in hits)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def brave_search(
    query: str,
    api_key: str,
    *,
    count: int = DEFAULT_COUNT,
    timeout: float = 15.0,
    opener: Any = None,
) -> list[SearchHit]:
    """Run one query against the Brave Web Search API.

    ``opener`` is injectable so tests never touch the network.
    """
    params = {"q": query[:MAX_QUERY_CHARS], "count": count, "text_decorations": "false"}
    request = urllib.request.Request(
        BRAVE_ENDPOINT + "?" + urllib.parse.urlencode(params),
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Subscription-Token": api_key,
            "User-Agent": "qq (+https://github.com/CrankingAI/qq-router)",
        },
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError(
                f"Brave Search rejected the API key ({exc.code})", hint=KEY_HINT
            ) from exc
        if exc.code == 429:
            raise NetworkError(
                "Brave Search rate limit hit (429)",
                hint="Wait a moment and retry, or check the plan at https://api-dashboard.search.brave.com.",
            ) from exc
        raise NetworkError(f"Brave Search returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise NetworkError(f"could not reach Brave Search: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise NetworkError(f"Brave Search request failed: {exc}") from exc
    except ValueError as exc:
        raise NetworkError("Brave Search returned a response that was not JSON") from exc

    results = ((payload.get("web") or {}).get("results")) or []
    hits = []
    for item in results:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        hits.append(
            SearchHit(
                title=clean_snippet(str(item.get("title", ""))),
                url=str(item["url"]),
                snippet=clean_snippet(str(item.get("description", ""))),
            )
        )
    return hits


def _parse_query(arguments: str) -> str:
    """Read the query out of the model's JSON arguments, tolerating junk."""
    try:
        data = json.loads(arguments or "{}")
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("query") or "").strip()


class WebSearch:
    """The search tool as the backend sees it: a definition plus a runner.

    One instance per question. It records every call it ran so the
    diagnostics can say what the model searched for and how long it took.
    """

    name = TOOL_NAME

    def __init__(
        self,
        api_key: str | None,
        *,
        count: int = DEFAULT_COUNT,
        max_rounds: int = MAX_SEARCH_ROUNDS,
        opener: Any = None,
    ) -> None:
        if not api_key:
            raise ConfigError("--search needs a Brave Search API key", hint=KEY_HINT)
        self.api_key = api_key
        self.count = count
        self.max_rounds = max_rounds
        self._opener = opener
        self.calls: list[SearchCall] = []

    @property
    def tool(self) -> dict[str, Any]:
        return TOOL_DEFINITION

    def run(self, arguments: str) -> str:
        """Execute one tool call and return the text the model gets back.

        A transient failure is handed back to the model as text rather than
        raised, so the user still gets an answer from what the model knows,
        with a note that the search failed. A rejected key is raised: that
        needs fixing, not working around.
        """
        query = _parse_query(arguments)
        call = SearchCall(query=query)
        self.calls.append(call)
        if not query:
            call.error = "empty query"
            return "Search failed: empty query. Answer from what you know."

        started = time.monotonic()
        try:
            hits = brave_search(query, self.api_key, count=self.count, opener=self._opener)
        except AuthError:
            raise
        except NetworkError as exc:
            call.latency = time.monotonic() - started
            call.error = exc.message
            return (
                f"Search failed: {exc.message}. Answer from what you know and say that "
                "the search failed."
            )
        call.latency = time.monotonic() - started
        call.hits = len(hits)
        return format_results(hits)
