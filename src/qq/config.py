"""Configuration resolution for qq.

Precedence, highest first:

1. command-line flags
2. ``QQ_*`` environment variables
3. the user config file
4. generic environment variables that other tools also read
   (``AZURE_OPENAI_*``, ``AZURE_TENANT_ID``, ``AZURE_SUBSCRIPTION_ID``,
   ``OPENROUTER_API_KEY``, ``BRAVE_API_KEY``), so an unconfigured machine
   works out of the box
5. built-in defaults

The generic variables sit *below* the config file on purpose. A shell profile
that exports ``AZURE_OPENAI_ENDPOINT`` for some other tool must not be able to
redirect a qq that has been configured; ``QQ_ENDPOINT`` or a flag is the
deliberate way to override.

The config file lives in an OS-appropriate user config directory and is written
with owner-only permissions. Nothing here ever writes to shell rc files.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import ConfigError

DEFAULT_DEPLOYMENT = "qq-router"
DEFAULT_TIMEOUT = 60.0

#: Backends qq can talk to. Both expose an OpenAI-compatible API.
PROVIDERS = ("azure", "openrouter")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: OpenRouter's auto-router, the closest equivalent to Azure's model-router.
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"

#: Rounds of searching the model may do before the tool is taken away.
#:
#: Measured on 2026-09-19 against qq-dev, eight questions at a cap of 10:
#: stable questions searched not at all, most current ones settled in one or
#: two, and the one that went the distance spent all ten refining a query for
#: a fact the snippets never stated - 25k input tokens, 135s, and a different
#: (invented) answer each run. Three leaves room for a second attempt at a bad
#: first query without paying for that.
DEFAULT_SEARCH_ROUNDS = 3

#: Ceiling on ``search_rounds``. Not a recommendation: a single ten-round
#: question was enough to trip the deployment's tokens-per-minute quota on its
#: own, which then rate limits the next question too.
MAX_SEARCH_ROUNDS = 10

#: OpenRouter cost tiers. These are percentile bands rather than ceilings, so a
#: tier excludes models cheaper than the band as well as models above it.
COST_TIERS = ("low", "medium", "high", "xhigh", "max")

#: Config keys a user may set via ``qq config set``.
SETTABLE_KEYS = (
    "provider",
    "endpoint",
    "deployment",
    "auth",
    "api_key",
    "openrouter_api_key",
    "openrouter_model",
    "tenant",
    "subscription",
    "model",
    "api",
    "router",
    "cost_tier",
    "allowed_models",
    "search",
    "search_rounds",
    "brave_api_key",
    "fallback",
    "timeout",
)

#: Notes written above a key in the config file. The file is regenerated on
#: every 'qq config set', so a note here survives where a hand-written comment
#: would not - and the number worth knowing is the one measurement behind the
#: default, which nothing else in the file could tell you.
KEY_NOTES = {
    "search_rounds": (
        "# Searches the model may run per question (1-10, default 3).",
        "# Measured: past 3 it mostly re-refines a query it has already",
        "# answered, and long searches trip the router's rate limit.",
    ),
}

#: Keys whose values must never be printed.
SECRET_KEYS = ("api_key", "openrouter_api_key", "brave_api_key")

AUTH_MODES = ("auto", "entra", "key")

#: API surfaces. "auto" resolves to "chat", the only surface model-router
#: supports; "responses" is for direct model deployments that advertise it.
API_SURFACES = ("auto", "chat", "responses")


def config_dir() -> Path:
    """Return the user config directory for qq."""
    override = os.environ.get("QQ_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "qq"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "qq"


def config_path() -> Path:
    """Return the path of the qq config file."""
    return config_dir() / "config.toml"


def read_config_file(path: Path | None = None) -> dict[str, object]:
    """Read the config file. A missing file is not an error."""
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"could not read config file {path}: {exc}",
            hint="Fix the file by hand, or delete it and run 'qq config set ...' again.",
        ) from exc
    return {k: v for k, v in data.items() if not isinstance(v, dict)}


def _toml_escape(value: str) -> str:
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return out


def write_config_file(values: dict[str, object], path: Path | None = None) -> Path:
    """Write the config file atomically with owner-only permissions."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(stat.S_IRWXU)

    lines = ["# qq configuration", "# Written by 'qq config set'. Safe to edit by hand.", ""]
    for key in sorted(values):
        value = values[key]
        if value is None or value == "":
            continue
        notes = KEY_NOTES.get(key, ())
        if notes:
            # Blank line first: a note is a heading for the key below it, not a
            # trailer on the key above it.
            lines.append("")
            lines.extend(notes)
        if isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f'{key} = "{_toml_escape(str(value))}"')
    body = "\n".join(lines) + "\n"

    tmp = path.with_suffix(".toml.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def normalize_endpoint(raw: str) -> str:
    """Normalize any Foundry/Azure OpenAI endpoint to its ``/openai/v1`` base URL.

    Accepts the resource endpoint in any of the shapes Azure hands out
    (``*.cognitiveservices.azure.com``, ``*.services.ai.azure.com``,
    ``*.openai.azure.com``) and tolerates a base URL that already carries the
    ``/openai/v1`` suffix, so pasting from the portal or from another tool's
    config both work. A Foundry project endpoint
    (``.../api/projects/<name>``) works the same way: its OpenAI-compatible
    route hangs off it at ``/openai/v1``.
    """
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    for suffix in ("/openai/v1", "/openai"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value + "/openai/v1"


def default_deployment(provider: str) -> str:
    """What a provider addresses when nothing names a deployment or model."""
    return OPENROUTER_DEFAULT_MODEL if provider == "openrouter" else DEFAULT_DEPLOYMENT


def redact(value: str | None) -> str:
    """Render a secret as a fixed mask. Never reveals length or content."""
    if not value:
        return "(not set)"
    return "***redacted***"


@dataclass
class Settings:
    """Fully resolved runtime settings."""

    provider: str = "azure"
    endpoint: str = ""
    deployment: str = DEFAULT_DEPLOYMENT
    api_key: str | None = None
    auth: str = "auto"
    tenant: str | None = None
    #: Azure subscription id. Used only to disambiguate which Azure CLI account
    #: to ask for a token, never sent anywhere.
    subscription: str | None = None
    #: What the Azure deployment is backed by, e.g. "model-router:2025-11-18".
    #: Recorded by scripts/setup-cli.sh: the inference API does not report it,
    #: so it reflects configuration time rather than live state.
    router: str | None = None
    model: str | None = None
    #: OpenRouter cost tier, the rough analogue of Azure's routing mode.
    cost_tier: str | None = None
    #: Comma-separated patterns restricting what the auto-router may choose.
    allowed_models: str | None = None
    #: Offer the model a web search tool (Brave). Off by default: most terminal
    #: questions do not need it, and it sends the model's query to a second
    #: service.
    search: bool = False
    #: How many rounds of searching one question may pay for.
    search_rounds: int = DEFAULT_SEARCH_ROUNDS
    brave_api_key: str | None = None
    #: Fall back to the other provider when this one is rate limited. On by
    #: default: it costs nothing until a 429 arrives, and a standby that has to
    #: be switched on before it helps is a standby nobody has switched on.
    fallback: bool = True
    api: str = "auto"
    timeout: float = DEFAULT_TIMEOUT
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def effective_provider(self) -> str:
        return self.provider if self.provider in PROVIDERS else "azure"

    @property
    def base_url(self) -> str:
        """The URL the OpenAI client is pointed at.

        OpenRouter publishes one fixed base URL, so it needs no configuration
        and never gets the Azure ``/openai/v1`` suffix appended.
        """
        if self.effective_provider == "openrouter":
            return OPENROUTER_BASE_URL
        return normalize_endpoint(self.endpoint) if self.endpoint else ""

    @property
    def is_project_endpoint(self) -> bool:
        """Whether the Azure endpoint is a Foundry *project* endpoint.

        A Foundry account publishes two OpenAI-compatible routes. The account
        endpoint (``*.openai.azure.com``) only speaks Chat Completions to a
        ``model-router`` deployment. The project endpoint
        (``*.services.ai.azure.com/api/projects/<name>``) also accepts the
        Responses API, which is what tools, and therefore ``--search``, need.
        """
        return self.effective_provider == "azure" and "/api/projects/" in self.endpoint

    @property
    def is_configured(self) -> bool:
        """Whether this provider has enough to be worth sending a request to.

        OpenRouter needs a key. Azure needs an endpoint; its credentials can
        come from ``az login`` rather than the config file, so the key is not
        the test.
        """
        if self.effective_provider == "openrouter":
            return bool(self.api_key)
        return bool(self.endpoint)

    @property
    def allowed_model_list(self) -> list[str]:
        if not self.allowed_models:
            return []
        return [item.strip() for item in self.allowed_models.split(",") if item.strip()]

    @property
    def effective_auth(self) -> str:
        """Resolve ``auto`` into the mode that will actually be used.

        OpenRouter only understands bearer API keys; there is no Entra path, so
        the mode is fixed rather than inferred.
        """
        if self.effective_provider == "openrouter":
            return "key"
        if self.auth == "key":
            return "key"
        if self.auth == "entra":
            return "entra"
        return "key" if self.api_key else "entra"

    @property
    def effective_api(self) -> str:
        """Resolve ``auto`` into the surface that will actually be used.

        Responses is the default wherever it works: on OpenRouter, and on
        Azure through a Foundry project endpoint. The one place it does not
        work is a ``model-router`` deployment addressed via the bare account
        endpoint, which answers ``400 The requested operation is unsupported``,
        so that case stays on Chat Completions.
        """
        if self.api in ("responses", "chat"):
            return self.api
        if self.effective_provider == "openrouter" or self.is_project_endpoint:
            return "responses"
        return "chat"

    def require_endpoint(self) -> str:
        if self.effective_provider == "openrouter":
            return self.base_url
        if not self.endpoint:
            raise ConfigError(
                "no endpoint configured",
                hint=(
                    "Run './scripts/configure.sh' after deploying, or set QQ_ENDPOINT "
                    "to your Foundry resource endpoint."
                ),
            )
        return self.base_url


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def _truthy(value: object, key: str) -> bool:
    """Read a boolean that may arrive as a flag, an env var, or a TOML value."""
    if value is None or isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ConfigError(f"invalid value for {key}: {value!r}", hint="Use true or false.")


def _search_rounds(value: object) -> int:
    """Read the search round cap, which arrives as a flag, env var or TOML value.

    Bounded at both ends. Zero rounds would offer the model a tool it is never
    allowed to use, and a typo'd 50 would turn one question into a rate limit
    for the next one.
    """
    if value is None or value == "":
        return DEFAULT_SEARCH_ROUNDS
    try:
        rounds = int(str(value).strip())
    except ValueError:
        raise ConfigError(
            f"invalid value for search_rounds: {value!r}",
            hint=f"Use a whole number from 1 to {MAX_SEARCH_ROUNDS}.",
        ) from None
    if not 1 <= rounds <= MAX_SEARCH_ROUNDS:
        raise ConfigError(
            f"search_rounds must be between 1 and {MAX_SEARCH_ROUNDS}, not {rounds}",
            hint=f"{DEFAULT_SEARCH_ROUNDS} is the default; past that it mostly refines queries.",
        )
    return rounds


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def resolve(
    *,
    env: dict[str, str] | None = None,
    file_values: dict[str, object] | None = None,
    model: str | None = None,
    deployment: str | None = None,
    endpoint: str | None = None,
    auth: str | None = None,
    tenant: str | None = None,
    api: str | None = None,
    router: str | None = None,
    provider: str | None = None,
    cost_tier: str | None = None,
    search: bool | None = None,
    search_rounds: int | None = None,
    fallback: bool | None = None,
    timeout: float | None = None,
) -> Settings:
    """Resolve settings from flags, environment, and file values.

    ``env`` and ``file_values`` are injectable so the precedence rules can be
    tested without touching the real environment or the real home directory.
    """
    environ = os.environ if env is None else env
    values = dict(file_values if file_values is not None else read_config_file())
    sources: dict[str, str] = {}

    def pick(
        key: str,
        flag: object,
        env_names: tuple[str, ...],
        file_key: str | None = None,
        fallback_env: tuple[str, ...] = (),
    ) -> object:
        """Resolve one setting.

        ``env_names`` are qq's own variables and outrank the config file.
        ``fallback_env`` are generic variables other tools also read; they
        make an unconfigured machine work but never override a config file
        the user wrote. ``file_key`` lets a provider keep its own persisted
        value under a different name, so an Azure endpoint in the config file
        can never be picked up by an OpenRouter request.
        """
        stored = file_key or key
        if flag not in (None, ""):
            sources[key] = "flag"
            return flag
        for name in env_names:
            got = environ.get(name)
            if got:
                sources[key] = f"env:{name}"
                return got
        if values.get(stored) not in (None, ""):
            sources[key] = "config file"
            return values[stored]
        for name in fallback_env:
            got = environ.get(name)
            if got:
                sources[key] = f"env:{name}"
                return got
        sources[key] = "default"
        return None

    resolved_provider = pick("provider", provider, ("QQ_PROVIDER",))
    provider_name = str(resolved_provider or "azure").lower()
    if provider_name not in PROVIDERS:
        raise ConfigError(
            f"unknown provider {provider_name!r}",
            hint=f"Valid providers: {', '.join(PROVIDERS)}.",
        )
    is_openrouter = provider_name == "openrouter"

    if is_openrouter:
        # OpenRouter publishes one fixed base URL. Reading the config file's
        # endpoint here would point an OpenRouter key at an Azure host.
        resolved_endpoint = endpoint
        sources["endpoint"] = "flag" if endpoint else "default"
        resolved_deployment = pick(
            "deployment", deployment, ("QQ_DEPLOYMENT", "QQ_MODEL"), file_key="openrouter_model"
        )
    else:
        resolved_endpoint = pick(
            "endpoint", endpoint, ("QQ_ENDPOINT",), fallback_env=("AZURE_OPENAI_ENDPOINT",)
        )
        resolved_deployment = pick("deployment", deployment, ("QQ_DEPLOYMENT",))

    # Each provider keeps its own key, so both can be configured at once and
    # QQ_PROVIDER=openrouter works without clobbering the Azure setup.
    if is_openrouter:
        resolved_key = pick(
            "openrouter_api_key",
            None,
            ("QQ_OPENROUTER_API_KEY",),
            fallback_env=("OPENROUTER_API_KEY",),
        )
    else:
        resolved_key = pick(
            "api_key", None, ("QQ_API_KEY",), fallback_env=("AZURE_OPENAI_API_KEY",)
        )
        # A generic key belongs with the generic endpoint it was exported
        # alongside. Pairing it with an endpoint from qq's own config would
        # send some other resource's key here, and with auth=auto it would
        # also silently switch off Entra. So it counts only when the endpoint
        # came from the same place.
        if (
            sources.get("api_key") == "env:AZURE_OPENAI_API_KEY"
            and sources.get("endpoint") != "env:AZURE_OPENAI_ENDPOINT"
        ):
            resolved_key = None
            sources["api_key"] = "default"
    resolved_auth = pick("auth", auth, ("QQ_AUTH",))
    resolved_tenant = pick("tenant", tenant, ("QQ_TENANT_ID",), fallback_env=("AZURE_TENANT_ID",))
    resolved_subscription = pick(
        "subscription", None, ("QQ_SUBSCRIPTION_ID",), fallback_env=("AZURE_SUBSCRIPTION_ID",)
    )
    resolved_model = pick("model", model, ("QQ_MODEL",))
    resolved_api = pick("api", api, ("QQ_API",))
    resolved_router = pick("router", router, ("QQ_ROUTER",))
    resolved_cost_tier = pick("cost_tier", cost_tier, ("QQ_COST_TIER",))
    resolved_allowed = pick("allowed_models", None, ("QQ_ALLOWED_MODELS",))
    resolved_search = pick("search", search, ("QQ_SEARCH",))
    # The Brave key is provider-neutral: the same search tool serves both
    # backends, so it lives under one name and one pair of variables.
    resolved_brave = pick(
        "brave_api_key", None, ("QQ_BRAVE_API_KEY",), fallback_env=("BRAVE_API_KEY",)
    )
    resolved_timeout = pick("timeout", timeout, ("QQ_TIMEOUT",))

    auth_mode = str(resolved_auth or "auto").lower()
    if auth_mode not in AUTH_MODES:
        raise ConfigError(
            f"invalid auth mode {auth_mode!r}",
            hint=f"Valid modes: {', '.join(AUTH_MODES)}.",
        )

    api_surface = str(resolved_api or "auto").lower()
    if api_surface not in API_SURFACES:
        raise ConfigError(
            f"invalid API surface {api_surface!r}",
            hint=f"Valid surfaces: {', '.join(API_SURFACES)}.",
        )

    tier = str(resolved_cost_tier).lower() if resolved_cost_tier else None
    if tier is not None and tier not in COST_TIERS:
        raise ConfigError(
            f"invalid cost tier {tier!r}",
            hint=f"Valid tiers: {', '.join(COST_TIERS)}.",
        )

    try:
        timeout_value = float(resolved_timeout) if resolved_timeout else DEFAULT_TIMEOUT
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid timeout {resolved_timeout!r}") from exc

    search_value = _truthy(resolved_search, "search")
    resolved_rounds = pick("search_rounds", search_rounds, ("QQ_SEARCH_ROUNDS",))
    rounds_value = _search_rounds(resolved_rounds)
    resolved_fallback = pick("fallback", fallback, ("QQ_FALLBACK",))
    fallback_value = True if resolved_fallback is None else _truthy(resolved_fallback, "fallback")

    return Settings(
        provider=provider_name,
        endpoint=str(resolved_endpoint or ""),
        deployment=str(resolved_deployment or default_deployment(provider_name)),
        api_key=str(resolved_key) if resolved_key else None,
        auth=auth_mode,
        tenant=str(resolved_tenant) if resolved_tenant else None,
        subscription=str(resolved_subscription) if resolved_subscription else None,
        model=str(resolved_model) if resolved_model else None,
        api=api_surface,
        router=str(resolved_router) if resolved_router else None,
        cost_tier=tier,
        allowed_models=str(resolved_allowed) if resolved_allowed else None,
        search=search_value,
        search_rounds=rounds_value,
        brave_api_key=str(resolved_brave) if resolved_brave else None,
        fallback=fallback_value,
        timeout=timeout_value,
        sources=sources,
    )


def standby(settings: Settings, overrides: dict[str, object] | None = None) -> Settings | None:
    """The other provider's settings, when it can stand in for this one.

    Resolved by a second full pass rather than by copying the primary's
    settings: each provider keeps its own key, its own default model and its
    own endpoint rules, and a copy would point an OpenRouter request at an
    Azure deployment name.

    Returns None when there is nothing to stand by with, or when standing by
    would answer a different question from the one asked. ``overrides`` is the
    same mapping that produced ``settings``, so a flag that names one provider
    is visible here.
    """
    overrides = dict(overrides or {})
    if not settings.fallback:
        return None
    # A question that names where to send it - an explicit model, deployment
    # or endpoint - is not a question another provider can answer.
    if settings.model or overrides.get("deployment") or overrides.get("endpoint"):
        return None
    other = "openrouter" if settings.effective_provider == "azure" else "azure"
    for key in ("model", "deployment", "endpoint", "provider"):
        overrides.pop(key, None)
    candidate = resolve(provider=other, **overrides)  # type: ignore[arg-type]
    if not candidate.is_configured:
        return None
    # QQ_DEPLOYMENT and QQ_MODEL name no provider, so a value exported for the
    # primary arrives here as the standby's model slug: 'qq-router' is an Azure
    # deployment, not something OpenRouter can serve. Anything that did not
    # come from the standby's own configuration goes back to its default.
    if candidate.sources.get("deployment", "").startswith("env:"):
        candidate = replace(candidate, deployment=default_deployment(other))
    # A search question needs the Responses API, and a standby that cannot run
    # the tool would fail the moment it was asked.
    if settings.search and candidate.effective_api != "responses":
        return None
    return candidate
