"""Azure AI Foundry backend.

Holds everything that is specific to Azure: the Entra ID credential chain, the
token audience, and the client construction. The request and response handling
lives in :mod:`qq.client` and is shared with every other provider.

**Which endpoint, which API.** A Foundry account publishes two OpenAI-compatible
routes, and they are not equivalent for a ``model-router`` deployment:

* The account endpoint, ``https://<account>.openai.azure.com/openai/v1``,
  speaks Chat Completions to the router. Calling ``/responses`` there returns
  ``400 The requested operation is unsupported``, and the router's capability
  list on the management plane agrees: ``chatCompletion`` and ``router``, no
  ``responses`` (verified September 2026, every region).
* The project endpoint,
  ``https://<account>.services.ai.azure.com/api/projects/<project>/openai/v1``,
  accepts both, including the Responses API with tools, which is what
  ``--search`` needs. It is what ``deploy.sh`` provisions and
  ``setup-cli.sh`` records.

``Settings.effective_api`` picks Responses on a project endpoint and Chat
Completions on an account endpoint, so either configuration works. Direct
model deployments such as ``gpt-5.6-luna`` support Responses on both routes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .client import Backend
from .errors import ConfigError

#: Token audience for Microsoft Entra ID against Azure AI Foundry. The older
#: https://cognitiveservices.azure.com/.default audience is for classic Azure
#: OpenAI resources.
ENTRA_SCOPE = "https://ai.azure.com/.default"


def build_credential(tenant: str | None = None, subscription: str | None = None) -> Any:
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

    if not tenant and not subscription:
        return DefaultAzureCredential()

    # Pinning the tenant alone is not enough. The Azure CLI resolves a tenant
    # against whatever account is currently the default, so if that default
    # belongs to an unrelated directory the token request fails even though the
    # right credential is still cached. Naming the subscription picks the
    # correct account directly, which is what 'az account get-access-token
    # --subscription' does.
    #
    # The two must not be combined: 'az' refuses "--subscription" together
    # with "--tenant", and when the CLI credential errors out the chain falls
    # through to slower fallbacks that can hang waiting on an interactive
    # sign-in. A subscription implies its tenant, so it wins when present.
    cli_kwargs: dict[str, Any] = (
        {"subscription": subscription} if subscription else {"tenant_id": tenant}
    )

    return ChainedTokenCredential(
        AzureCliCredential(**cli_kwargs),
        DefaultAzureCredential(
            interactive_browser_tenant_id=tenant,
            shared_cache_tenant_id=tenant,
            visual_studio_code_tenant_id=tenant,
            workload_identity_tenant_id=tenant,
        ),
    )


def entra_token_provider(
    scope: str = ENTRA_SCOPE,
    tenant: str | None = None,
    stats: dict[str, Any] | None = None,
    subscription: str | None = None,
) -> Callable[[], str]:
    """Build a bearer-token provider backed by Entra ID.

    Returned uncalled to the OpenAI client, which invokes it per request. A
    cached token is reused across processes until shortly before it expires,
    which removes roughly 0.65s of ``az`` startup from every question.
    """
    from . import tokencache

    key = tokencache.cache_key(f"{tenant or ''}|{subscription or ''}", scope)
    credential: Any = None

    def provider() -> str:
        nonlocal credential
        cached = tokencache.load(key)
        if cached:
            if stats is not None:
                stats["token_cache"] = "hit"
            return cached
        if credential is None:
            credential = build_credential(tenant, subscription)
        access = credential.get_token(scope)
        tokencache.store(key, access.token, access.expires_on)
        if stats is not None:
            stats["token_cache"] = "off" if tokencache.disabled() else "miss"
        return access.token

    return provider


class AzureFoundryBackend(Backend):
    """Azure AI Foundry, speaking Chat Completions or Responses."""

    provider = "azure"
    provider_label = "Azure"

    @property
    def tenant_label(self) -> str | None:
        return self.settings.tenant

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
            credential = entra_token_provider(
                tenant=self.settings.tenant,
                stats=self._auth_stats,
                subscription=self.settings.subscription,
            )

        self._cached = OpenAI(
            base_url=base_url,
            api_key=credential,
            timeout=self.settings.timeout,
            max_retries=2,
        )
        return self._cached


#: Names kept from when qq only spoke to Azure.
FoundryBackend = AzureFoundryBackend
ResponsesBackend = AzureFoundryBackend
