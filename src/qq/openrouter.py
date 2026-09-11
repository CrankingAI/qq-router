"""OpenRouter backend.

OpenRouter is the closest thing to Azure's Model Router outside Azure, and the
mapping is close enough that qq treats them as two configurations of the same
idea rather than two separate tools:

===========================  ======================================
Azure AI Foundry             OpenRouter
===========================  ======================================
``model-router`` deployment  the ``openrouter/auto`` model
``routingMode``              ``cost_tier`` on the auto-router plugin
``routerModels`` subset      ``allowed_models`` on the same plugin
``response.model``           ``response.model``
===========================  ======================================

The two differ in how they choose. Azure classifies difficulty and picks a
model. OpenRouter classifies the prompt into one of roughly thirty task types,
then ranks candidates by what the OpenRouter community actually spent on that
task type over a trailing week, filtered by your cost tier.

Two OpenRouter behaviours need explicit handling, both covered below: the API
answers ``200 OK`` before the upstream provider has succeeded, so failures can
arrive in the body of a successful response; and the real per-request cost is
reported inline, which is worth surfacing because it is the number a user of a
paid aggregator actually cares about.

Billing note: OpenRouter is billed by OpenRouter, not against Azure credits.
"""

from __future__ import annotations

from typing import Any

from .client import Backend, _attr, _extra
from .config import OPENROUTER_DEFAULT_MODEL
from .errors import ConfigError

#: Sent for leaderboard attribution on openrouter.ai. Optional, and carries no
#: user data: it identifies the tool, not the person running it.
ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://github.com/CrankingAI/qq-router",
    "X-OpenRouter-Title": "qq",
    "X-OpenRouter-Categories": "cli-agent",
}

#: Opt-in header that adds an ``openrouter_metadata`` object to responses,
#: carrying the upstream provider, the routing strategy and the task type the
#: auto-router classified the prompt as. Without it there is no supported way
#: to learn which provider served the request short of a second round trip.
METADATA_HEADER = {"X-OpenRouter-Metadata": "enabled"}

AUTO_ROUTER_PLUGIN_ID = "auto-router"


class OpenRouterBackend(Backend):
    """OpenRouter, speaking Chat Completions."""

    provider = "openrouter"
    provider_label = "OpenRouter"

    def _build_client(self) -> Any:
        if self._cached is not None:
            return self._cached

        from openai import OpenAI

        from .telemetry import setup as setup_telemetry

        setup_telemetry()

        if not self.settings.api_key:
            raise ConfigError(
                "no OpenRouter API key configured",
                hint=(
                    "Set QQ_OPENROUTER_API_KEY or OPENROUTER_API_KEY, or run "
                    "'qq config set openrouter_api_key <key>'. Create one at "
                    "https://openrouter.ai/keys."
                ),
            )

        self._cached = OpenAI(
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            timeout=self.settings.timeout,
            max_retries=2,
            default_headers={**ATTRIBUTION_HEADERS, **METADATA_HEADER},
        )
        self._auth_stats["token_cache"] = "n/a"
        return self._cached

    @property
    def uses_auto_router(self) -> bool:
        """Whether the target is OpenRouter's auto-router rather than one model."""
        return self.target.startswith(OPENROUTER_DEFAULT_MODEL)

    @property
    def router_label(self) -> str | None:
        """Describe the routing in the same slot Azure uses for its router.

        Unlike Azure, this is observable rather than recorded at setup time:
        the model slug says whether auto-routing is in play.
        """
        if not self.uses_auto_router:
            return "direct"
        tier = self.settings.cost_tier
        return f"{OPENROUTER_DEFAULT_MODEL}:{tier}" if tier else OPENROUTER_DEFAULT_MODEL

    def request_options(self) -> dict[str, Any]:
        """Attach the auto-router plugin when auto-routing is in use.

        The plugin is what carries the cost tier and the model restrictions.
        Sending it for a directly addressed model would be meaningless, so it
        is omitted there, and omitted entirely when neither knob is set so that
        the default request stays as plain as possible.
        """
        if not self.uses_auto_router:
            return {}

        plugin: dict[str, Any] = {"id": AUTO_ROUTER_PLUGIN_ID}
        if self.settings.cost_tier:
            plugin["cost_tier"] = self.settings.cost_tier
        allowed = self.settings.allowed_model_list
        if allowed:
            plugin["allowed_models"] = allowed
        if len(plugin) == 1:
            return {}
        return {"extra_body": {"plugins": [plugin]}}

    def collect_provider_meta(self, meta: dict[str, Any], response: Any, usage: Any = None) -> None:
        """Pull cost and routing metadata out of an OpenRouter response.

        ``cost`` is always present. The rest comes from ``openrouter_metadata``,
        which only appears because of the opt-in header, so every read tolerates
        its absence.
        """
        if usage is not None:
            cost = _extra(usage, "cost")
            if isinstance(cost, (int, float)):
                meta["cost"] = float(cost)

        metadata = _extra(response, "openrouter_metadata")
        if not isinstance(metadata, dict):
            return

        strategy = metadata.get("strategy")
        if strategy:
            meta["strategy"] = strategy

        endpoints = metadata.get("endpoints")
        if isinstance(endpoints, dict):
            for entry in endpoints.get("available") or []:
                if isinstance(entry, dict) and entry.get("selected"):
                    if entry.get("provider"):
                        meta["upstream"] = entry["provider"]
                    break

        # The auto-router records the task type it classified the prompt as,
        # which is the single most interesting thing about a routing decision.
        for stage in metadata.get("pipeline") or []:
            if not isinstance(stage, dict):
                continue
            data = stage.get("data")
            if isinstance(data, dict) and data.get("task_type"):
                meta["task_type"] = data["task_type"]
                break

        # Undocumented on success, but present on error chunks; harmless to use
        # as a fallback when the endpoint list did not name a provider.
        if "upstream" not in meta:
            fallback = _attr(response, "provider")
            if isinstance(fallback, str) and fallback:
                meta["upstream"] = fallback
