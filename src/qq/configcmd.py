"""The `qq config` subcommand.

Reads and writes the user config file. Secret values can be written but are
never read back out in plain text: ``show`` and ``get`` both mask them.
"""

from __future__ import annotations

from .config import (
    SECRET_KEYS,
    SETTABLE_KEYS,
    config_path,
    read_config_file,
    redact,
    resolve,
    write_config_file,
)
from .errors import EXIT_OK, UsageError

CONFIG_USAGE = f"""\
usage: qq config [command]

  show              show the resolved configuration and where each value came from
  get KEY           show one resolved value
  set KEY VALUE     write KEY to the config file
  unset KEY         remove KEY from the config file
  path              print the config file path

keys: {", ".join(SETTABLE_KEYS)}
"""


def _standby_line(settings) -> str:
    """Which provider answers a rate limit, in the words 'qq doctor' uses."""
    from .config import standby

    if not settings.fallback:
        return "off"
    other = standby(settings)
    if other is None:
        return "none configured"
    return f"{other.effective_provider} ({other.deployment}) on a 429"


def _display(key: str, value: object) -> str:
    if value in (None, ""):
        return "(not set)"
    if key in SECRET_KEYS:
        return redact(str(value))
    return str(value)


def run_config(args: list[str]) -> int:
    verb = args[0] if args else "show"
    rest = args[1:]

    if verb in ("show", "list"):
        return _show()
    if verb == "path":
        print(config_path())
        return EXIT_OK
    if verb == "get":
        if len(rest) != 1:
            raise UsageError("usage: qq config get KEY")
        return _get(rest[0])
    if verb == "set":
        if len(rest) != 2:
            raise UsageError("usage: qq config set KEY VALUE")
        return _set(rest[0], rest[1])
    if verb == "unset":
        if len(rest) != 1:
            raise UsageError("usage: qq config unset KEY")
        return _unset(rest[0])

    raise UsageError(f"unknown config command {verb!r}", hint=CONFIG_USAGE)


def _check_key(key: str) -> str:
    if key not in SETTABLE_KEYS:
        raise UsageError(
            f"unknown config key {key!r}",
            hint=f"Valid keys: {', '.join(SETTABLE_KEYS)}.",
        )
    return key


def _show() -> int:
    settings = resolve()
    path = config_path()
    print(f"config file: {path}{'' if path.exists() else '  (not created yet)'}")
    print()
    rows = [
        ("provider", settings.provider),
        ("endpoint", settings.endpoint),
        ("base url", settings.base_url),
        ("deployment", settings.deployment),
        ("model override", settings.model),
        ("auth mode", f"{settings.auth} -> {settings.effective_auth}"),
        ("api surface", f"{settings.api} -> {settings.effective_api}"),
        ("tenant", settings.tenant),
        ("router", settings.router),
        ("cost tier", settings.cost_tier),
        ("allowed models", settings.allowed_models),
        (
            "openrouter key" if settings.effective_provider == "openrouter" else "api key",
            settings.api_key,
        ),
        ("search", "on" if settings.search else "off"),
        ("brave key", settings.brave_api_key),
        ("standby", _standby_line(settings)),
        ("timeout", settings.timeout),
    ]
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        key = {
            "api key": "api_key",
            "openrouter key": "openrouter_api_key",
            "model override": "model",
            "base url": "endpoint",
            "api surface": "api",
            "cost tier": "cost_tier",
            "allowed models": "allowed_models",
            "brave key": "brave_api_key",
            "standby": "fallback",
        }.get(name, name)
        source = settings.sources.get(key, "")
        suffix = f"   [{source}]" if source and source != "default" else ""
        print(f"  {name.ljust(width)}  {_display(key, value)}{suffix}")
    return EXIT_OK


#: Config keys a provider stores under its own name but which resolve onto a
#: shared Settings field. Without this, 'qq config get openrouter_api_key'
#: looked up a Settings attribute that does not exist and always said "not set",
#: even with a key stored in the file.
_KEY_TO_FIELD = {
    "openrouter_api_key": "api_key",
    "openrouter_model": "deployment",
}


def _get(key: str) -> int:
    _check_key(key)
    settings = resolve()
    value = getattr(settings, _KEY_TO_FIELD.get(key, key), None)
    print(_display(key, value))
    return EXIT_OK


def _set(key: str, value: str) -> int:
    _check_key(key)
    values = read_config_file()
    values[key] = value
    path = write_config_file(values)
    print(f"set {key} in {path}")
    if key in SECRET_KEYS:
        print("the value was written with owner-only permissions and will never be printed")
    return EXIT_OK


def _unset(key: str) -> int:
    _check_key(key)
    values = read_config_file()
    if key not in values:
        print(f"{key} was not set")
        return EXIT_OK
    del values[key]
    path = write_config_file(values)
    print(f"removed {key} from {path}")
    return EXIT_OK
