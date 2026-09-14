"""Configuration resolution for qq.

Precedence, highest first:

1. command-line flags
2. ``QQ_*`` environment variables
3. ``AZURE_OPENAI_*`` environment variables (so an existing Azure setup works)
4. the user config file
5. built-in defaults

The config file lives in an OS-appropriate user config directory and is written
with owner-only permissions. Nothing here ever writes to shell rc files.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

DEFAULT_DEPLOYMENT = "qq-router"
DEFAULT_TIMEOUT = 60.0

#: Backends qq can talk to. Both expose an OpenAI-compatible API.
PROVIDERS = ("azure", "openrouter")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: OpenRouter's auto-router, the closest equivalent to Azure's model-router.
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"

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
    "timeout",
)

#: Keys whose values must never be printed.
SECRET_KEYS = ("api_key", "openrouter_api_key")

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
    config both work.
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

        Chat Completions is the default because model-router does not support
        the Responses API; asking for it against the router returns HTTP 400.
        """
        return "responses" if self.api == "responses" else "chat"

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
    ) -> object:
        """Resolve one setting. ``file_key`` lets a provider keep its own
        persisted value under a different name, so an Azure endpoint in the
        config file can never be picked up by an OpenRouter request."""
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
        resolved_endpoint = pick("endpoint", endpoint, ("QQ_ENDPOINT", "AZURE_OPENAI_ENDPOINT"))
        resolved_deployment = pick("deployment", deployment, ("QQ_DEPLOYMENT",))

    # Each provider keeps its own key, so both can be configured at once and
    # QQ_PROVIDER=openrouter works without clobbering the Azure setup.
    if is_openrouter:
        resolved_key = pick(
            "openrouter_api_key", None, ("QQ_OPENROUTER_API_KEY", "OPENROUTER_API_KEY")
        )
    else:
        resolved_key = pick("api_key", None, ("QQ_API_KEY", "AZURE_OPENAI_API_KEY"))
    resolved_auth = pick("auth", auth, ("QQ_AUTH",))
    resolved_tenant = pick("tenant", tenant, ("QQ_TENANT_ID", "AZURE_TENANT_ID"))
    resolved_subscription = pick(
        "subscription", None, ("QQ_SUBSCRIPTION_ID", "AZURE_SUBSCRIPTION_ID")
    )
    resolved_model = pick("model", model, ("QQ_MODEL",))
    resolved_api = pick("api", api, ("QQ_API",))
    resolved_router = pick("router", router, ("QQ_ROUTER",))
    resolved_cost_tier = pick("cost_tier", cost_tier, ("QQ_COST_TIER",))
    resolved_allowed = pick("allowed_models", None, ("QQ_ALLOWED_MODELS",))
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

    default_deployment = OPENROUTER_DEFAULT_MODEL if is_openrouter else DEFAULT_DEPLOYMENT

    return Settings(
        provider=provider_name,
        endpoint=str(resolved_endpoint or ""),
        deployment=str(resolved_deployment or default_deployment),
        api_key=str(resolved_key) if resolved_key else None,
        auth=auth_mode,
        tenant=str(resolved_tenant) if resolved_tenant else None,
        subscription=str(resolved_subscription) if resolved_subscription else None,
        model=str(resolved_model) if resolved_model else None,
        api=api_surface,
        router=str(resolved_router) if resolved_router else None,
        cost_tier=tier,
        allowed_models=str(resolved_allowed) if resolved_allowed else None,
        timeout=timeout_value,
        sources=sources,
    )
