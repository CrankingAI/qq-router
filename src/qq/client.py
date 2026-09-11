"""Talk to Azure AI Foundry.

This is the only module that knows about Azure. Everything above it deals in
``Answer`` objects, so a second backend could be added without touching
argument parsing or terminal output.

**Why two API surfaces.** Azure exposes both Chat Completions and the newer
Responses API on the ``/openai/v1`` route, but they are not supported by the
same models. As of September 2026 the ``model-router`` model advertises only
the ``chatCompletion`` capability in every region; calling ``/openai/v1/responses``
against a router deployment returns ``400 The requested operation is unsupported``.
Direct model deployments such as ``gpt-5.6-luna`` advertise ``responses`` as well.

So qq defaults to Chat Completions, which the router requires, and offers
``--api responses`` for direct deployments. Both surfaces report the model that
actually served the request, which is what ``--verbose`` prints.

Check what a model supports before switching::

    az cognitiveservices model list --location eastus2 \\
      --query "[?model.name=='model-router'].model.capabilities" -o json

Two other deliberate choices:

* ``openai`` and ``azure_identity`` are imported inside functions. Importing
  them costs a few hundred milliseconds, and ``qq --help`` should not pay it.
* Entra authentication passes the token provider *callable* as ``api_key``.
  The v1 client invokes it per request, so the bearer token refreshes instead
  of expiring an hour into a long-lived shell.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import AuthError, ConfigError, NetworkError, QQError
from .prompt import SYSTEM_INSTRUCTION

#: Token audience for Microsoft Entra ID against Azure AI Foundry. The older
#: https://cognitiveservices.azure.com/.default audience is for classic Azure
#: OpenAI resources.
ENTRA_SCOPE = "https://ai.azure.com/.default"

#: API surfaces qq can speak. "auto" resolves to "chat", the only surface the
#: model router supports.
API_SURFACES = ("auto", "chat", "responses")


@dataclass
class Answer:
    """One completed answer, plus what the diagnostics line needs."""

    text: str
    model: str | None = None
    deployment: str = ""
    latency: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None

    def diagnostics(self) -> str:
        """Render the ``--verbose`` line. Contains no secrets by construction."""
        parts = [f"deployment={self.deployment or '?'}", f"model={self.model or '?'}"]
        parts.append(f"latency={self.latency:.2f}s")
        if self.input_tokens is not None and self.output_tokens is not None:
            parts.append(f"tokens={self.input_tokens}in/{self.output_tokens}out")
        return "[" + " ".join(parts) + "]"


def _attr(obj: Any, name: str) -> Any:
    """Read an attribute from an SDK model or the equivalent key from a dict."""
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return value


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
        for block in _attr(item, "content") or []:
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


def build_credential(tenant: str | None = None) -> Any:
    """Build an Entra ID credential, optionally pinned to one tenant.

    Pinning matters more than it looks. ``DefaultAzureCredential`` asks the
    Azure CLI for a token using the CLI's *current* subscription, which is
    global mutable state shared by every shell on the machine. If that default
    points at a different tenant than your Foundry resource, Azure rejects the
    call with "Tenant provided in token does not match resource token" even
    though you are perfectly well logged in.

    With a tenant configured, the CLI credential is asked for that tenant
    explicitly and the rest of the default chain is pinned to it too.
    """
    try:
        from azure.identity import (
            AzureCliCredential,
            ChainedTokenCredential,
            DefaultAzureCredential,
        )
    except ImportError as exc:  # pragma: no cover - packaging guarantees this
        raise ConfigError(
            "azure-identity is not installed, so Entra ID sign-in is unavailable",
            hint="Reinstall qq, or set QQ_API_KEY to use API-key authentication.",
        ) from exc

    if not tenant:
        return DefaultAzureCredential()

    return ChainedTokenCredential(
        AzureCliCredential(tenant_id=tenant),
        DefaultAzureCredential(
            interactive_browser_tenant_id=tenant,
            shared_cache_tenant_id=tenant,
            visual_studio_code_tenant_id=tenant,
            workload_identity_tenant_id=tenant,
        ),
    )


def entra_token_provider(scope: str = ENTRA_SCOPE, tenant: str | None = None) -> Callable[[], str]:
    """Build a bearer-token provider backed by Entra ID.

    Returned uncalled to the OpenAI client, which invokes it per request. A
    cached token is reused across processes until shortly before it expires,
    which removes roughly 0.65s of ``az`` startup from every question.
    """
    from . import tokencache

    key = tokencache.cache_key(tenant, scope)
    credential: Any = None

    def provider() -> str:
        nonlocal credential
        cached = tokencache.load(key)
        if cached:
            return cached
        if credential is None:
            credential = build_credential(tenant)
        access = credential.get_token(scope)
        tokencache.store(key, access.token, access.expires_on)
        return access.token

    return provider


def _short(exc: Any, limit: int = 300) -> str:
    message = getattr(exc, "message", None) or str(exc)
    message = " ".join(str(message).split())
    return message if len(message) <= limit else message[: limit - 1] + "…"


def translate_error(exc: Exception) -> QQError:
    """Map an SDK exception onto a qq error with an actionable hint."""
    import openai

    if isinstance(exc, openai.AuthenticationError):
        return AuthError(
            "Azure rejected the credentials (401)",
            hint=(
                "For key auth, check QQ_API_KEY. For Entra auth, run 'az login' and make "
                "sure you hold the Foundry User role on the Foundry account."
            ),
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return AuthError(
            "Azure accepted the identity but denied access (403)",
            hint="Grant the Foundry User role on the Foundry account, then retry.",
        )
    if isinstance(exc, openai.NotFoundError):
        return ConfigError(
            "deployment not found (404)",
            hint="Check QQ_DEPLOYMENT matches a deployment on this resource: 'qq doctor'.",
        )
    if isinstance(exc, openai.RateLimitError):
        return NetworkError(
            "rate limited by Azure (429)",
            hint="Wait a moment, or raise routerCapacity and redeploy the Bicep.",
        )
    if isinstance(exc, openai.APITimeoutError):
        return NetworkError(
            "the request to Azure timed out",
            hint="Retry, or raise the timeout with --timeout / QQ_TIMEOUT.",
        )
    if isinstance(exc, openai.APIConnectionError):
        return NetworkError(
            f"could not reach Azure: {exc}",
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
                "This deployment does not support that API surface. model-router speaks "
                "Chat Completions only, so drop '--api responses'."
            )
        return QQError(f"Azure rejected the request (400): {detail}", hint=hint)
    if isinstance(exc, openai.APIStatusError):
        return QQError(f"Azure returned HTTP {exc.status_code}: {_short(exc)}")
    if isinstance(exc, openai.OpenAIError):
        return QQError(f"OpenAI client error: {exc}")
    return QQError(str(exc))


class FoundryBackend:
    """Azure AI Foundry backend, speaking Chat Completions or Responses."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cached: Any = None

    # -- wiring -------------------------------------------------------------

    def _build_client(self) -> Any:
        if self._cached is not None:
            return self._cached

        from openai import OpenAI

        from .telemetry import setup as setup_telemetry

        setup_telemetry()

        base_url = self.settings.require_endpoint()
        mode = self.settings.effective_auth

        if mode == "key":
            if not self.settings.api_key:
                raise ConfigError(
                    "auth mode is 'key' but no API key is configured",
                    hint="Set QQ_API_KEY, or switch to Entra with QQ_AUTH=entra.",
                )
            credential: Any = self.settings.api_key
        else:
            # Passed uncalled on purpose: the SDK invokes it per request, which
            # keeps the bearer token fresh.
            credential = entra_token_provider(tenant=self.settings.tenant)

        self._cached = OpenAI(
            base_url=base_url,
            api_key=credential,
            timeout=self.settings.timeout,
            max_retries=2,
        )
        return self._cached

    @property
    def target(self) -> str:
        """The deployment (or explicit model) this call will address."""
        return self.settings.model or self.settings.deployment

    @property
    def surface(self) -> str:
        """The API surface this call will use."""
        return self.settings.effective_api

    # -- public API ---------------------------------------------------------

    def ask(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_delta: Callable[[str], None] | None = None,
    ) -> Answer:
        """Send one prompt and return the answer.

        When ``stream`` is true, ``on_delta`` receives text fragments as they
        arrive and the returned ``Answer`` still carries the full text plus the
        routing metadata from the terminal event.
        """
        client = self._build_client()
        target = self.target
        started = time.monotonic()

        try:
            if self.surface == "responses":
                text, model, usage = self._via_responses(client, target, prompt, stream, on_delta)
            else:
                text, model, usage = self._via_chat(client, target, prompt, stream, on_delta)
        except QQError:
            raise
        except Exception as exc:
            raise translate_error(exc) from exc

        if not text:
            raise QQError(
                "Azure returned an empty answer",
                hint="Retry, or run with --verbose to see the routing details.",
            )
        return Answer(
            text=text,
            model=model,
            deployment=target,
            latency=time.monotonic() - started,
            input_tokens=usage[0],
            output_tokens=usage[1],
        )

    # -- Chat Completions ---------------------------------------------------

    def _via_chat(
        self,
        client: Any,
        target: str,
        prompt: str,
        stream: bool,
        on_delta: Callable[[str], None] | None,
    ) -> tuple[str, str | None, tuple[int | None, int | None]]:
        messages = [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ]
        if not stream:
            response = client.chat.completions.create(model=target, messages=messages)
            return extract_chat_text(response), _attr(response, "model"), _chat_usage(response)

        chunks: list[str] = []
        model: str | None = None
        usage: tuple[int | None, int | None] = (None, None)
        events = client.chat.completions.create(
            model=target,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
        )
        for event in events:
            model = _attr(event, "model") or model
            if _attr(event, "usage") is not None:
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
    ) -> tuple[str, str | None, tuple[int | None, int | None]]:
        if not stream:
            response = client.responses.create(
                model=target, instructions=SYSTEM_INSTRUCTION, input=prompt
            )
            return (
                extract_responses_text(response),
                _attr(response, "model"),
                _responses_usage(response),
            )

        chunks: list[str] = []
        final: Any = None
        events = client.responses.create(
            model=target, instructions=SYSTEM_INSTRUCTION, input=prompt, stream=True
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

        text = "".join(chunks).strip()
        if not text and final is not None:
            text = extract_responses_text(final)
        model = _attr(final, "model") if final is not None else None
        usage = _responses_usage(final) if final is not None else (None, None)
        return text, model, usage


#: Backwards-compatible alias from when qq only spoke the Responses API.
ResponsesBackend = FoundryBackend
