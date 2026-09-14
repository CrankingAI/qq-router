"""OpenRouter backend: routing options, response metadata, and its 200-OK trap."""

import types

import openai
import pytest

from qq.client import Answer, build_backend, translate_error
from qq.config import OPENROUTER_BASE_URL, OPENROUTER_DEFAULT_MODEL, resolve
from qq.errors import AuthError, ConfigError, QQError
from qq.openrouter import OpenRouterBackend


def settings(**kwargs):
    """Build resolved OpenRouter settings from config-file values.

    The model is stored under ``openrouter_model``, not ``deployment``, so an
    Azure deployment name in the same config file can never be sent to
    OpenRouter. The helper mirrors that.
    """
    base = {"provider": "openrouter", "openrouter_api_key": "sk-or-test"}
    if "deployment" in kwargs:
        base["openrouter_model"] = kwargs.pop("deployment")
    base.update(kwargs)
    return resolve(env={}, file_values=base)


# --- configuration ----------------------------------------------------------


def test_openrouter_needs_no_endpoint_configuration():
    s = settings()
    assert s.base_url == OPENROUTER_BASE_URL
    assert s.require_endpoint() == OPENROUTER_BASE_URL


def test_openrouter_defaults_to_the_auto_router():
    assert settings().deployment == OPENROUTER_DEFAULT_MODEL


def test_an_explicit_model_overrides_the_auto_router():
    assert settings(deployment="openai/gpt-5-nano").deployment == "openai/gpt-5-nano"


def test_openrouter_auth_is_always_key_even_if_entra_is_configured():
    assert settings(auth="entra").effective_auth == "key"


def test_each_provider_keeps_its_own_key_so_both_can_be_configured():
    env = {
        "QQ_PROVIDER": "openrouter",
        "QQ_API_KEY": "azure-key",
        "OPENROUTER_API_KEY": "openrouter-key",
    }
    assert resolve(env=env, file_values={}).api_key == "openrouter-key"
    env["QQ_PROVIDER"] = "azure"
    assert resolve(env=env, file_values={}).api_key == "azure-key"


def test_qq_prefixed_openrouter_key_wins():
    env = {
        "QQ_PROVIDER": "openrouter",
        "QQ_OPENROUTER_API_KEY": "preferred",
        "OPENROUTER_API_KEY": "fallback",
    }
    assert resolve(env=env, file_values={}).api_key == "preferred"


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError):
        resolve(env={"QQ_PROVIDER": "hal9000"}, file_values={})


def test_invalid_cost_tier_is_rejected():
    with pytest.raises(ConfigError):
        resolve(env={"QQ_PROVIDER": "openrouter", "QQ_COST_TIER": "cheapest"}, file_values={})


def test_build_backend_dispatches_on_provider():
    assert isinstance(build_backend(settings()), OpenRouterBackend)
    assert build_backend(resolve(env={}, file_values={})).provider == "azure"


def test_allowed_models_parses_a_comma_separated_list():
    s = settings(allowed_models="openai/gpt-5*, anthropic/*")
    assert s.allowed_model_list == ["openai/gpt-5*", "anthropic/*"]


# --- request options --------------------------------------------------------


def test_no_plugin_is_sent_when_no_routing_knobs_are_set():
    """Keep the default request as plain as possible."""
    assert OpenRouterBackend(settings()).request_options() == {}


def test_cost_tier_becomes_an_auto_router_plugin():
    options = OpenRouterBackend(settings(cost_tier="low")).request_options()
    plugin = options["extra_body"]["plugins"][0]
    assert plugin["id"] == "auto-router"
    assert plugin["cost_tier"] == "low"


def test_allowed_models_restrict_the_auto_router():
    backend = OpenRouterBackend(settings(cost_tier="medium", allowed_models="openai/*,qwen/*"))
    plugin = backend.request_options()["extra_body"]["plugins"][0]
    assert plugin["allowed_models"] == ["openai/*", "qwen/*"]


def test_routing_options_are_dropped_for_a_directly_addressed_model():
    """The auto-router plugin is meaningless when not auto-routing."""
    backend = OpenRouterBackend(settings(deployment="openai/gpt-5-nano", cost_tier="low"))
    assert backend.uses_auto_router is False
    assert backend.request_options() == {}


def test_router_label_describes_the_routing_in_use():
    assert OpenRouterBackend(settings()).router_label == OPENROUTER_DEFAULT_MODEL
    assert OpenRouterBackend(settings(cost_tier="high")).router_label == (
        f"{OPENROUTER_DEFAULT_MODEL}:high"
    )
    assert OpenRouterBackend(settings(deployment="openai/gpt-5-nano")).router_label == "direct"


def test_missing_key_fails_with_a_hint_pointing_at_the_key_page():
    backend = OpenRouterBackend(resolve(env={}, file_values={"provider": "openrouter"}))
    with pytest.raises(ConfigError) as excinfo:
        backend._build_client()
    assert "openrouter.ai/keys" in excinfo.value.hint


# --- response metadata ------------------------------------------------------


class ORUsage:
    def __init__(self, prompt, completion, cost=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.model_extra = {"cost": cost} if cost is not None else {}


def or_response(text="hi", model="anthropic/claude-sonnet-4.5", usage=None, metadata=None):
    message = types.SimpleNamespace(content=text)
    response = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message)],
        model=model,
        usage=usage,
        id="gen-abc123",
    )
    response.model_extra = {"openrouter_metadata": metadata} if metadata else {}
    return response


METADATA = {
    "strategy": "auto",
    "endpoints": {
        "available": [
            {"provider": "Together", "selected": False},
            {"provider": "Anthropic", "selected": True},
        ]
    },
    "pipeline": [{"data": {"task_type": "code:debugging"}}],
}


def _ask(backend, response):
    class FakeCompletions:
        def create(self, **kwargs):
            backend.last_kwargs = kwargs
            return response

    backend._cached = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions())
    )
    return backend.ask("why is this broken")


def test_cost_and_routing_metadata_reach_the_answer():
    backend = OpenRouterBackend(settings(cost_tier="low"))
    answer = _ask(backend, or_response(usage=ORUsage(15, 150, cost=0.000123), metadata=METADATA))

    assert answer.model == "anthropic/claude-sonnet-4.5"
    assert answer.cost == pytest.approx(0.000123)
    assert answer.upstream == "Anthropic"
    assert answer.strategy == "auto"
    assert answer.task_type == "code:debugging"
    assert answer.request_id == "gen-abc123"
    assert answer.provider == "openrouter"


def test_the_plugin_is_actually_sent_on_the_request():
    backend = OpenRouterBackend(settings(cost_tier="low", allowed_models="openai/*"))
    _ask(backend, or_response(usage=ORUsage(1, 1)))
    plugin = backend.last_kwargs["extra_body"]["plugins"][0]
    assert plugin == {"id": "auto-router", "cost_tier": "low", "allowed_models": ["openai/*"]}


def test_missing_metadata_is_tolerated():
    """openrouter_metadata only appears because of an opt-in header."""
    backend = OpenRouterBackend(settings())
    answer = _ask(backend, or_response(usage=ORUsage(1, 2)))
    assert answer.upstream is None
    assert answer.strategy is None
    assert answer.task_type is None


def test_cost_appears_at_level_two_and_upstream_at_level_three():
    answer = Answer(
        text="x",
        model="anthropic/claude-sonnet-4.5",
        deployment=OPENROUTER_DEFAULT_MODEL,
        latency=1.0,
        provider="openrouter",
        router=f"{OPENROUTER_DEFAULT_MODEL}:low",
        cost=0.000123,
        upstream="Anthropic",
        strategy="auto",
        task_type="code:debugging",
        host="openrouter.ai",
        api="chat",
        auth="key",
    )
    assert "cost=$0.000123" in answer.diagnostics(2)
    assert "provider=openrouter" in answer.diagnostics(2)
    assert "upstream=Anthropic" in answer.diagnostics(3)
    assert "task=code:debugging" in answer.diagnostics(3)
    assert "cost=" not in answer.diagnostics(1)


# --- the 200-OK-with-an-error trap -----------------------------------------


def test_an_error_body_behind_a_200_is_raised_not_swallowed():
    """OpenRouter answers 200 before the upstream provider has succeeded."""
    backend = OpenRouterBackend(settings())
    broken = types.SimpleNamespace(choices=[], model=None, usage=None, id="gen-x")
    broken.model_extra = {
        "error": {
            "code": 429,
            "message": "Rate limit exceeded",
            "metadata": {"error_type": "rate_limit_exceeded"},
        }
    }
    with pytest.raises(QQError) as excinfo:
        _ask(backend, broken)
    assert "Rate limit exceeded" in excinfo.value.message
    assert "429" in excinfo.value.message
    assert "rate_limit_exceeded" in excinfo.value.message
    assert "empty answer" not in excinfo.value.message


def test_a_mid_stream_error_chunk_is_raised():
    backend = OpenRouterBackend(settings())
    good = types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content="par"))],
        model="openai/gpt-5-nano",
        usage=None,
    )
    good.model_extra = {}
    bad = types.SimpleNamespace(choices=[], model=None, usage=None)
    bad.model_extra = {"error": {"code": 502, "message": "Provider is down"}}

    class FakeCompletions:
        def create(self, **kwargs):
            return iter([good, bad])

    backend._cached = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=FakeCompletions())
    )
    with pytest.raises(QQError) as excinfo:
        backend.ask("hi", stream=True, on_delta=lambda _: None)
    assert "Provider is down" in excinfo.value.message


def test_azure_responses_are_unaffected_by_the_inline_error_check():
    from qq.azure import AzureFoundryBackend

    backend = AzureFoundryBackend(
        resolve(env={}, file_values={"endpoint": "https://x.openai.azure.com"})
    )
    clean = types.SimpleNamespace(choices=[], model=None, usage=None)
    clean.model_extra = {}
    backend.check_inline_error(clean)  # must not raise


# --- error translation ------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self.request = object()


def _status_error(cls, status, message="boom"):
    return cls(message, response=_FakeHTTPResponse(status), body=None)


def test_auth_hint_points_at_the_openrouter_key_not_azure():
    translated = translate_error(_status_error(openai.AuthenticationError, 401), "openrouter")
    assert isinstance(translated, AuthError)
    assert "OPENROUTER_API_KEY" in translated.hint
    assert "az login" not in translated.hint


def test_insufficient_credits_is_reported_as_such():
    translated = translate_error(_status_error(openai.APIStatusError, 402), "openrouter")
    assert "credits" in translated.message
    assert "openrouter.ai/settings/credits" in translated.hint


def test_not_found_hint_mentions_model_restrictions():
    translated = translate_error(_status_error(openai.NotFoundError, 404), "openrouter")
    assert "allowed_models" in translated.hint


def test_azure_hints_are_unchanged():
    translated = translate_error(_status_error(openai.AuthenticationError, 401), "azure")
    assert "az login" in translated.hint


def test_an_azure_config_file_cannot_leak_into_an_openrouter_request():
    """The bug this guards: an Azure endpoint and deployment sitting in the
    config file were being used to send an OpenRouter key to an Azure host."""
    stored = {
        "provider": "openrouter",
        "openrouter_api_key": "sk-or-test",
        "endpoint": "https://my-foundry.openai.azure.com",
        "deployment": "qq-router",
        "router": "model-router:2025-11-18",
        "tenant": "some-tenant",
    }
    s = resolve(env={}, file_values=stored)
    assert s.base_url == OPENROUTER_BASE_URL
    assert s.deployment == OPENROUTER_DEFAULT_MODEL
    assert OpenRouterBackend(s).router_label == OPENROUTER_DEFAULT_MODEL


def test_an_openrouter_config_file_cannot_leak_into_an_azure_request():
    stored = {
        "provider": "azure",
        "endpoint": "https://my-foundry.openai.azure.com",
        "deployment": "qq-router",
        "openrouter_model": "openai/gpt-5-nano",
        "openrouter_api_key": "sk-or-test",
    }
    s = resolve(env={}, file_values=stored)
    assert s.deployment == "qq-router"
    assert s.api_key is None
    assert "openai.azure.com" in s.base_url


def test_tenant_is_azure_only_and_never_appears_in_openrouter_output():
    """Entra tenants are an Azure concept; leaking one into OpenRouter
    diagnostics implies a relationship that does not exist."""
    stored = {
        "provider": "openrouter",
        "openrouter_api_key": "sk-or-test",
        "tenant": "00000000-1111-2222-3333-444444444444",
    }
    backend = OpenRouterBackend(resolve(env={}, file_values=stored))
    assert backend.tenant_label is None

    answer = _ask(backend, or_response(usage=ORUsage(1, 1)))
    assert answer.tenant is None
    assert "tenant=" not in answer.diagnostics(3)


def test_azure_still_reports_its_tenant():
    from qq.azure import AzureFoundryBackend

    stored = {
        "endpoint": "https://x.openai.azure.com",
        "tenant": "00000000-1111-2222-3333-444444444444",
    }
    backend = AzureFoundryBackend(resolve(env={}, file_values=stored))
    assert backend.tenant_label == "00000000-1111-2222-3333-444444444444"
