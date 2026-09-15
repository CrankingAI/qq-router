"""Shared client layer.

Every backend qq speaks to exposes an OpenAI-compatible API, so the request
and response handling lives here once and each provider module supplies only
what actually differs: how the client is authenticated, and which vendor
extensions its responses carry.

Answer is the boundary. Everything above this layer, argument parsing and
terminal output, deals in Answer objects and knows nothing about which
provider produced one.

Two deliberate choices survive from the original Azure-only version:

* openai and any credential library are imported inside functions.
  Importing them costs a few hundred milliseconds, and qq --help should
  not pay it.
* Bearer credentials are handed to the SDK as a *callable* where the provider
  supports it, so the token refreshes per request instead of expiring an hour
  into a long-lived shell.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .errors import AuthError, ConfigError, NetworkError, QQError
from .prompt import SYSTEM_INSTRUCTION, system_instruction
from .search import SearchCall, WebSearch

#: API surfaces qq can speak. "auto" resolves to "chat", the only surface the
#: Azure model router supports and the one every provider implements.
API_SURFACES = ("auto", "chat", "responses")


#: Server-side timing fields Azure returns, in the order worth reading them,
#: mapped to the short labels used in the -vvv output.
SERVER_TIMING_FIELDS = (
    ("pre_inference_ms", "pre_inference"),
    ("engine_ttft_ms", "engine_ttft"),
    ("engine_ttlt_ms", "engine_ttlt"),
    ("engine_tbt_ms", "engine_tbt"),
    ("service_ttft_ms", "service_ttft"),
    ("service_ttlt_ms", "service_ttlt"),
    ("user_visible_ttft_ms", "visible_ttft"),
)


@dataclass
class Answer:
    """One completed answer, plus everything the diagnostics tiers can show.

    Fields are grouped by the verbosity level that reveals them. Anything the
    service did not return stays ``None`` and is omitted rather than printed
    as a placeholder, because a diagnostics line that invents fields is worse
    than one that is short.
    """

    text: str
    # -v
    provider: str = ""
    model: str | None = None
    deployment: str = ""
    latency: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    # -vv
    router: str | None = None
    cost: float | None = None
    host: str = ""
    api: str = ""
    auth: str = ""
    stream: bool = False
    request_id: str | None = None
    # -vvv
    server_timings: dict[str, int] = field(default_factory=dict)
    replica: str | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    token_cache: str | None = None
    tenant: str | None = None
    upstream: str | None = None
    strategy: str | None = None
    task_type: str | None = None
    # -v when --search is on: how many searches the model chose to run.
    # -vv adds the queries themselves, which is the interesting part.
    search_enabled: bool = False
    searches: list[SearchCall] = field(default_factory=list)

    def _tier1(self) -> str:
        # Provider leads. Once qq can talk to more than one backend, a line that
        # does not name the backend is ambiguous, and the deployment name only
        # implies it by convention.
        parts = []
        if self.provider:
            parts.append(f"provider={self.provider}")
        parts += [f"deployment={self.deployment or '?'}", f"model={self.model or '?'}"]
        parts.append(f"latency={self.latency:.2f}s")
        if self.input_tokens is not None and self.output_tokens is not None:
            parts.append(f"tokens={self.input_tokens}in/{self.output_tokens}out")
        if self.search_enabled:
            parts.append(f"search={len(self.searches)}")
        return "[" + " ".join(parts) + "]"

    def _tier2(self) -> str:
        parts = []
        # Recorded when the CLI was configured. The inference API does not
        # report what a deployment is backed by, so this cannot be derived
        # live; it is labelled as configuration, not observation.
        parts.append(f"router={self.router}" if self.router else "router=?")
        if self.host:
            parts.append(f"host={self.host}")
        parts.append(f"api={self.api or '?'}")
        parts.append(f"auth={self.auth or '?'}")
        parts.append(f"stream={'on' if self.stream else 'off'}")
        if self.cost is not None:
            # OpenRouter reports the real charge per request. Six decimals
            # because a terminal question routinely costs less than a cent.
            parts.append(f"cost=${self.cost:.6f}")
        if self.request_id:
            parts.append(f"request={self.request_id}")
        if self.searches:
            # What the model actually searched for, which is rarely what the
            # user typed and is the first thing to look at when an answer is off.
            parts.append("search_query=" + "|".join(json.dumps(c.query) for c in self.searches))
            parts.append(f"search_latency={sum(c.latency for c in self.searches):.2f}s")
            failed = [c.error for c in self.searches if c.error]
            if failed:
                parts.append(f"search_error={json.dumps(failed[0])}")
        return "[" + " ".join(parts) + "]"

    def _tier3(self) -> list[str]:
        lines = []
        if self.server_timings:
            timing = [
                f"{label}={self.server_timings[key]}ms"
                for key, label in SERVER_TIMING_FIELDS
                if key in self.server_timings
            ]
            if timing:
                lines.append("[server " + " ".join(timing) + "]")

        detail = []
        if self.upstream:
            detail.append(f"upstream={self.upstream}")
        if self.strategy:
            detail.append(f"strategy={self.strategy}")
        if self.task_type:
            detail.append(f"task={self.task_type}")
        if self.replica:
            detail.append(f"replica={self.replica}")
        if self.cached_tokens is not None:
            detail.append(f"cached={self.cached_tokens}")
        if self.reasoning_tokens is not None:
            detail.append(f"reasoning={self.reasoning_tokens}")
        if self.token_cache:
            detail.append(f"token_cache={self.token_cache}")
        if self.tenant:
            detail.append(f"tenant={self.tenant}")
        overhead = self.client_overhead
        if overhead is not None:
            detail.append(f"overhead={overhead:.2f}s")
        if detail:
            lines.append("[detail " + " ".join(detail) + "]")
        return lines

    @property
    def client_overhead(self) -> float | None:
        """Wall time not accounted for by the service's own total.

        Large values point at the local side: token acquisition, TLS setup, or
        a slow network path rather than a slow model.
        """
        total_ms = self.server_timings.get("service_ttlt_ms")
        if total_ms is None:
            return None
        return max(0.0, self.latency - (total_ms / 1000.0))

    def diagnostics(self, level: int = 1) -> str:
        """Render the diagnostics for a verbosity level.

        Levels are additive: level 2 emits the level 1 line unchanged and adds
        to it, so the familiar line never moves. Contains no secrets by
        construction; the API key is never a field here.
        """
        if level <= 0:
            return ""
        lines = [self._tier1()]
        if level >= 2:
            lines.append(self._tier2())
        if level >= 3:
            lines.extend(self._tier3())
        return "\n".join(lines)


def _attr(obj: Any, name: str) -> Any:
    """Read an attribute from an SDK model or the equivalent key from a dict."""
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return value


def _extra(obj: Any, name: str) -> Any:
    """Read a vendor extension the OpenAI schema does not define.

    Azure attaches ``routing`` and ``latency_checkpoint`` to responses. These
    are not part of the OpenAI schema, so the SDK parks them in ``model_extra``
    rather than exposing them as attributes. They are undocumented and may
    disappear, so every read is best-effort and every caller tolerates None.
    """
    if obj is None:
        return None
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict) and name in extra:
        return extra[name]
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _merge_meta(meta: dict[str, Any], response: Any, usage: Any = None) -> None:
    """Fold whatever diagnostics a response or stream chunk carries into meta."""
    if response is None:
        return
    request_id = _attr(response, "id")
    if request_id:
        meta["request_id"] = request_id

    routing = _extra(response, "routing")
    if isinstance(routing, dict) and routing.get("serving_endpoint"):
        meta["replica"] = routing["serving_endpoint"]

    # Non-streaming puts the timings under usage; streaming puts them at the
    # top level of the final chunk. Accept either.
    for source in (usage, response):
        checkpoint = _extra(source, "latency_checkpoint")
        if isinstance(checkpoint, dict):
            meta.setdefault("server_timings", {}).update(
                {k: v for k, v in checkpoint.items() if isinstance(v, (int, float))}
            )

    if usage is not None:
        prompt_details = _attr(usage, "prompt_tokens_details") or _attr(
            usage, "input_tokens_details"
        )
        cached = _attr(prompt_details, "cached_tokens")
        if cached is not None:
            meta["cached_tokens"] = cached

        completion_details = _attr(usage, "completion_tokens_details") or _attr(
            usage, "output_tokens_details"
        )
        reasoning = _attr(completion_details, "reasoning_tokens")
        if reasoning is not None:
            meta["reasoning_tokens"] = reasoning


def extract_responses_text(response: Any) -> str:
    """Pull the assistant text out of a Responses API result.

    ``output_text`` is the documented accessor; the manual walk is a fallback
    for shapes the SDK returns as plain dicts, so a working answer is never
    lost to an attribute error.
    """
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    chunks: list[str] = []
    for item in _attr(response, "output") or []:
        # Only message items carry the answer. Reasoning items also have text
        # content, and a tool-call round on OpenRouter returns reasoning with
        # no message; without this filter that reasoning would be printed as
        # if it were the answer.
        if _attr(item, "type") not in (None, "message"):
            continue
        for block in _attr(item, "content") or []:
            if _attr(block, "type") not in (None, "output_text"):
                continue
            block_text = _attr(block, "text")
            if isinstance(block_text, str):
                chunks.append(block_text)
    return "".join(chunks).strip()


def extract_chat_text(response: Any) -> str:
    """Pull the assistant text out of a Chat Completions result."""
    chunks: list[str] = []
    for choice in _attr(response, "choices") or []:
        message = _attr(choice, "message")
        content = _attr(message, "content") if message is not None else None
        if isinstance(content, str):
            chunks.append(content)
    return "".join(chunks).strip()


def _responses_usage(response: Any) -> tuple[int | None, int | None]:
    usage = _attr(response, "usage")
    if usage is None:
        return None, None
    return _attr(usage, "input_tokens"), _attr(usage, "output_tokens")


def _chat_usage(response: Any) -> tuple[int | None, int | None]:
    usage = _attr(response, "usage")
    if usage is None:
        return None, None
    return _attr(usage, "prompt_tokens"), _attr(usage, "completion_tokens")


def _short(exc: Any, limit: int = 300) -> str:
    message = getattr(exc, "message", None) or str(exc)
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[: limit - 1] + "…"


#: Per-provider remediation text. Keeping it in one table makes it obvious
#: when a provider is missing a hint for a failure mode that can happen to it.
_AUTH_HINTS = {
    "azure": (
        "For key auth, check QQ_API_KEY. For Entra auth, run 'az login' and make "
        "sure you hold the Foundry User role on the Foundry account."
    ),
    "openrouter": (
        "Check QQ_OPENROUTER_API_KEY or OPENROUTER_API_KEY. Create a key at "
        "https://openrouter.ai/keys."
    ),
}
_DENIED_HINTS = {
    "azure": "Grant the Foundry User role on the Foundry account, then retry.",
    "openrouter": "The key may be disabled, or the request was blocked by moderation.",
}
_NOT_FOUND_HINTS = {
    "azure": "Check QQ_DEPLOYMENT matches a deployment on this resource: 'qq doctor'.",
    "openrouter": (
        "Either the model slug is wrong, or your allowed_models restrictions matched "
        "nothing. Check 'qq config show'."
    ),
}


def translate_error(exc: Exception, provider: str = "azure") -> QQError:
    """Map an SDK exception onto a qq error with an actionable hint."""
    import openai

    label = "OpenRouter" if provider == "openrouter" else "Azure"

    if isinstance(exc, openai.AuthenticationError):
        return AuthError(
            f"{label} rejected the credentials (401)",
            hint=_AUTH_HINTS.get(provider, _AUTH_HINTS["azure"]),
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return AuthError(
            f"{label} accepted the identity but denied access (403)",
            hint=_DENIED_HINTS.get(provider, _DENIED_HINTS["azure"]),
        )
    if isinstance(exc, openai.NotFoundError):
        return ConfigError(
            f"{'model' if provider == 'openrouter' else 'deployment'} not found (404)",
            hint=_NOT_FOUND_HINTS.get(provider, _NOT_FOUND_HINTS["azure"]),
        )
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 402:
        return QQError(
            "OpenRouter reports insufficient credits (402)",
            hint="Top up at https://openrouter.ai/settings/credits.",
        )
    if isinstance(exc, openai.RateLimitError):
        return NetworkError(
            f"rate limited by {label} (429)",
            hint="Wait a moment, or raise routerCapacity and redeploy the Bicep.",
        )
    if isinstance(exc, openai.APITimeoutError):
        return NetworkError(
            f"the request to {label} timed out",
            hint="Retry, or raise the timeout with --timeout / QQ_TIMEOUT.",
        )
    if isinstance(exc, openai.APIConnectionError):
        return NetworkError(
            f"could not reach {label}: {exc}",
            hint="Check network connectivity and that QQ_ENDPOINT is correct.",
        )
    if isinstance(exc, openai.BadRequestError):
        detail = _short(exc)
        hint = None
        if "tenant" in detail.lower():
            hint = (
                "The Entra token came from the wrong tenant. Set QQ_TENANT_ID to the tenant "
                "that owns the Foundry resource (az account show --query tenantId -o tsv)."
            )
        elif "unsupported" in detail.lower():
            hint = (
                "This deployment does not support that API surface here. model-router "
                "accepts the Responses API only through a Foundry project endpoint "
                "(https://<account>.services.ai.azure.com/api/projects/<project>); on the "
                "account endpoint it speaks Chat Completions only. Switch the endpoint, "
                "or drop '--api responses'."
            )
        return QQError(f"{label} rejected the request (400): {detail}", hint=hint)
    if isinstance(exc, openai.APIStatusError):
        return QQError(f"{label} returned HTTP {exc.status_code}: {_short(exc)}")
    if isinstance(exc, openai.OpenAIError):
        return QQError(f"OpenAI client error: {exc}")
    return QQError(str(exc))


class Backend:
    """An OpenAI-compatible chat backend.

    Subclasses supply provider, provider_label and _build_client.
    Everything else, including both API surfaces, streaming and diagnostics
    collection, is shared.
    """

    #: Short machine-readable provider id, used in diagnostics and error hints.
    provider = "openai"
    #: Human-readable name, used in error messages.
    provider_label = "The service"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cached: Any = None
        self._auth_stats: dict[str, Any] = {}

    def _build_client(self) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def request_options(self) -> dict[str, Any]:
        """Extra kwargs to attach to every request.

        Providers use this for vendor headers and body fields. The default is
        nothing, which keeps the Azure path byte-identical to a plain call.
        """
        return {}

    @property
    def router_label(self) -> str | None:
        """What the target deployment or model is backed by, if known."""
        return self.settings.router

    @property
    def tenant_label(self) -> str | None:
        """Entra tenant, where the provider has one. Most do not."""
        return None

    def collect_provider_meta(self, meta: dict[str, Any], response: Any, usage: Any = None) -> None:
        """Fold provider-specific response extras into meta. Default: nothing."""

    def check_inline_error(self, payload: Any) -> None:
        """Raise if the body carries an error despite a 2xx status.

        OpenRouter returns HTTP 200 as soon as an upstream provider accepts the
        request, so a later failure arrives as an ``error`` object in the body
        with no ``choices``. The SDK does not raise for that, and without this
        check the user would see "returned an empty answer" instead of the real
        reason. Azure never does this, so the check is a no-op there.
        """
        error = _extra(payload, "error")
        if not error:
            return
        if isinstance(error, dict):
            message = error.get("message") or str(error)
            code = error.get("code")
            metadata = error.get("metadata") or {}
            kind = metadata.get("error_type") if isinstance(metadata, dict) else None
        else:
            message, code, kind = str(error), None, None
        prefix = f"{self.provider_label} error"
        if code is not None:
            prefix += f" ({code})"
        if kind:
            prefix += f" [{kind}]"
        raise QQError(f"{prefix}: {message}")

    @property
    def target(self) -> str:
        """The deployment (or explicit model) this call will address."""
        return self.settings.model or self.settings.deployment

    @property
    def surface(self) -> str:
        """The API surface this call will use."""
        return self.settings.effective_api

    @property
    def host(self) -> str:
        """Hostname of the configured endpoint, for diagnostics."""
        from urllib.parse import urlparse

        base = self.settings.base_url
        return urlparse(base).netloc if base else ""

    # -- public API ---------------------------------------------------------

    def ask(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
        search: WebSearch | None = None,
    ) -> Answer:
        """Send one prompt and return the answer.

        When ``stream`` is true, ``on_delta`` receives text fragments as they
        arrive and the returned ``Answer`` still carries the full text plus the
        routing metadata from the terminal event.

        When ``search`` is given, the model is offered it as a tool and may
        call it before answering. That needs the Responses API; Chat
        Completions has a tool protocol of its own that qq does not maintain
        a second copy of the loop for.
        """
        if search is not None and self.surface != "responses":
            raise ConfigError(
                "web search needs the Responses API",
                hint="Drop '--api chat', or on Azure point the endpoint at a Foundry project.",
            )
        client = self._build_client()
        target = self.target
        meta: dict[str, Any] = {}
        started = time.monotonic()

        try:
            if self.surface == "responses":
                text, model, usage = self._via_responses(
                    client, target, prompt, stream, on_delta, meta, search
                )
            else:
                text, model, usage = self._via_chat(client, target, prompt, stream, on_delta, meta)
        except QQError:
            raise
        except Exception as exc:
            raise translate_error(exc, self.provider) from exc

        if not text:
            raise QQError(
                f"{self.provider_label} returned an empty answer",
                hint="Retry, or run with --verbose to see the routing details.",
            )
        return Answer(
            text=text,
            model=model,
            deployment=target,
            latency=time.monotonic() - started,
            input_tokens=usage[0],
            output_tokens=usage[1],
            provider=self.provider,
            router=self.router_label,
            cost=meta.get("cost"),
            host=self.host,
            api=self.surface,
            auth=self.settings.effective_auth,
            stream=stream,
            request_id=meta.get("request_id"),
            server_timings=meta.get("server_timings", {}),
            replica=meta.get("replica"),
            cached_tokens=meta.get("cached_tokens"),
            reasoning_tokens=meta.get("reasoning_tokens"),
            token_cache=self._auth_stats.get("token_cache"),
            tenant=self.tenant_label,
            upstream=meta.get("upstream"),
            strategy=meta.get("strategy"),
            task_type=meta.get("task_type"),
            search_enabled=search is not None,
            searches=list(search.calls) if search is not None else [],
        )

    # -- Chat Completions ---------------------------------------------------

    def _via_chat(
        self,
        client: Any,
        target: str,
        prompt: str,
        stream: bool,
        on_delta: Callable[[str], None] | None,
        meta: dict[str, Any],
    ) -> tuple[str, str | None, tuple[int | None, int | None]]:
        messages = [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ]
        if not stream:
            response = client.chat.completions.create(
                model=target, messages=messages, **self.request_options()
            )
            self.check_inline_error(response)
            _merge_meta(meta, response, _attr(response, "usage"))
            self.collect_provider_meta(meta, response, _attr(response, "usage"))
            return extract_chat_text(response), _attr(response, "model"), _chat_usage(response)

        chunks: list[str] = []
        model: str | None = None
        usage: tuple[int | None, int | None] = (None, None)
        events = client.chat.completions.create(
            model=target,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            **self.request_options(),
        )
        for event in events:
            self.check_inline_error(event)
            model = _attr(event, "model") or model
            event_usage = _attr(event, "usage")
            _merge_meta(meta, event, event_usage)
            self.collect_provider_meta(meta, event, event_usage)
            if event_usage is not None:
                usage = _chat_usage(event)
            for choice in _attr(event, "choices") or []:
                delta = _attr(choice, "delta")
                piece = _attr(delta, "content") if delta is not None else None
                if isinstance(piece, str) and piece:
                    chunks.append(piece)
                    if on_delta:
                        on_delta(piece)
        return "".join(chunks).strip(), model, usage

    # -- Responses ----------------------------------------------------------

    def _via_responses(
        self,
        client: Any,
        target: str,
        prompt: str,
        stream: bool,
        on_delta: Callable[[str], None] | None,
        meta: dict[str, Any],
        search: WebSearch | None = None,
    ) -> tuple[str, str | None, tuple[int | None, int | None]]:
        """One question, possibly several requests.

        Without a search tool this is a single call. With one, the model may
        reply with a ``function_call`` instead of text; qq runs the search,
        appends the call and its output to the conversation, and asks again.
        After ``search.max_rounds`` rounds it sets ``tool_choice`` to ``none``
        so the model has to answer with what it has found.

        The conversation is resent in full each round rather than chained with
        ``previous_response_id``. That works on every provider and does not
        depend on the service storing responses, and the prompts are small.
        Token counts are summed across rounds, because the user is paying for
        all of them.
        """
        instructions = system_instruction(search is not None)
        conversation: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        chunks: list[str] = []
        model: str | None = None
        tokens_in: int | None = None
        tokens_out: int | None = None
        rounds = 0

        while True:
            options: dict[str, Any] = {}
            if search is not None:
                options["tools"] = [search.tool]
                if rounds >= search.max_rounds:
                    options["tool_choice"] = "none"
            # A plain question is sent as a plain string, so the request stays
            # byte-identical to what qq sent before tools existed.
            input_value: Any = prompt if len(conversation) == 1 else conversation

            if stream:
                text, final = self._stream_responses(
                    client, target, instructions, input_value, on_delta, options
                )
            else:
                final = client.responses.create(
                    model=target,
                    instructions=instructions,
                    input=input_value,
                    **options,
                    **self.request_options(),
                )
                self.check_inline_error(final)
                text = extract_responses_text(final)

            if text:
                chunks.append(text)
            if final is not None:
                usage = _attr(final, "usage")
                _merge_meta(meta, final, usage)
                self.collect_provider_meta(meta, final, usage)
                model = _attr(final, "model") or model
                got_in, got_out = _responses_usage(final)
                tokens_in = _add(tokens_in, got_in)
                tokens_out = _add(tokens_out, got_out)

            calls = _function_calls(final)
            if search is None or not calls or options.get("tool_choice") == "none":
                break
            for call in calls:
                call_id = _attr(call, "call_id")
                arguments = _attr(call, "arguments") or "{}"
                output = search.run(arguments)
                conversation.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": _attr(call, "name") or search.name,
                        "arguments": arguments,
                    }
                )
                conversation.append(
                    {"type": "function_call_output", "call_id": call_id, "output": output}
                )
            rounds += 1

        return "".join(chunks).strip(), model, (tokens_in, tokens_out)

    def _stream_responses(
        self,
        client: Any,
        target: str,
        instructions: str,
        input_value: Any,
        on_delta: Callable[[str], None] | None,
        options: dict[str, Any],
    ) -> tuple[str, Any]:
        """Stream one Responses request. Returns the text and the final response.

        A round that ends in a tool call carries no text deltas, so nothing is
        printed until the model actually answers.
        """
        chunks: list[str] = []
        final: Any = None
        events = client.responses.create(
            model=target,
            instructions=instructions,
            input=input_value,
            stream=True,
            **options,
            **self.request_options(),
        )
        for event in events:
            kind = _attr(event, "type") or ""
            if kind == "response.output_text.delta":
                piece = _attr(event, "delta") or ""
                if piece:
                    chunks.append(piece)
                    if on_delta:
                        on_delta(piece)
            elif kind in ("response.completed", "response.incomplete", "response.failed"):
                final = _attr(event, "response")
        if final is not None:
            self.check_inline_error(final)
        text = "".join(chunks).strip()
        if not text and final is not None:
            text = extract_responses_text(final)
        return text, final


def _function_calls(response: Any) -> list[Any]:
    """The tool calls a Responses result asks for, in order."""
    return [
        item
        for item in (_attr(response, "output") or [])
        if (_attr(item, "type") or "") == "function_call"
    ]


def _add(total: int | None, more: int | None) -> int | None:
    """Sum token counts across rounds, staying None until one is reported."""
    if more is None:
        return total
    return more if total is None else total + more


def build_backend(settings: Settings) -> Backend:
    """Pick a backend from the resolved settings.

    Imported lazily so that selecting one provider never pays the import cost
    of the other's credential stack.
    """
    provider = settings.effective_provider
    if provider == "openrouter":
        from .openrouter import OpenRouterBackend

        return OpenRouterBackend(settings)

    from .azure import AzureFoundryBackend

    return AzureFoundryBackend(settings)
