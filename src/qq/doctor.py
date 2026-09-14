"""The `qq doctor` subcommand.

Walks the chain from "is qq installed" to "can I actually get an answer",
stopping at the first thing that is broken so the output points at one problem
rather than a wall of cascading failures.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass

from . import __version__
from .config import config_path, redact, resolve
from .errors import EXIT_ERROR, EXIT_OK

OK = "ok"
WARN = "warn"
FAIL = "fail"

MARKS = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[fail]"}


@dataclass
class Check:
    status: str
    name: str
    detail: str
    hint: str = ""


def _emit(check: Check) -> None:
    print(f"{MARKS[check.status]} {check.name}: {check.detail}")
    if check.hint and check.status != OK:
        print(f"       hint: {check.hint}")


def _check_install() -> Check:
    return Check(
        OK,
        "install",
        f"qq {__version__} on Python {platform.python_version()} ({sys.executable})",
    )


def _check_dependencies() -> Check:
    missing = []
    versions = []
    for module, label in (("openai", "openai"), ("azure.identity", "azure-identity")):
        try:
            mod = __import__(module, fromlist=["__version__"])
        except ImportError:
            missing.append(label)
            continue
        versions.append(f"{label} {getattr(mod, '__version__', '?')}")
    if missing:
        return Check(
            FAIL,
            "dependencies",
            f"missing: {', '.join(missing)}",
            "Reinstall qq: uv tool install --force git+https://github.com/CrankingAI/qq-router",
        )
    return Check(OK, "dependencies", ", ".join(versions))


def _check_provider(settings) -> Check:
    label = "Azure AI Foundry" if settings.effective_provider == "azure" else "OpenRouter"
    source = settings.sources.get("provider", "default")
    return Check(OK, "provider", f"{settings.effective_provider} ({label})  [{source}]")


def _check_endpoint(settings) -> Check:
    if settings.effective_provider == "openrouter":
        return Check(OK, "endpoint", settings.base_url)
    if not settings.endpoint:
        return Check(
            FAIL,
            "endpoint",
            "not configured",
            "Run ./scripts/configure.sh, or set QQ_ENDPOINT to your Foundry endpoint.",
        )
    source = settings.sources.get("endpoint", "?")
    return Check(OK, "endpoint", f"{settings.base_url}  [{source}]")


def _check_deployment(settings) -> Check:
    source = settings.sources.get("deployment", "default")
    if settings.effective_provider == "openrouter":
        tier = f" cost_tier={settings.cost_tier}" if settings.cost_tier else ""
        return Check(OK, "model", f"{settings.deployment}{tier}  [{source}]")
    if source == "default":
        return Check(
            WARN,
            "deployment",
            f"{settings.deployment} (using the built-in default)",
            "Set QQ_DEPLOYMENT if your router deployment has a different name.",
        )
    return Check(OK, "deployment", f"{settings.deployment}  [{source}]")


def _check_auth(settings) -> Check:
    mode = settings.effective_auth
    if mode == "key":
        key_name = (
            "openrouter_api_key" if settings.effective_provider == "openrouter" else "api_key"
        )
        source = settings.sources.get(key_name, "?")
        if not settings.api_key:
            return Check(
                FAIL,
                "auth",
                "no API key configured",
                (
                    "Set QQ_OPENROUTER_API_KEY, or run 'qq config set openrouter_api_key <key>'."
                    if settings.effective_provider == "openrouter"
                    else "Set QQ_API_KEY, or switch to Entra with QQ_AUTH=entra."
                ),
            )
        return Check(OK, "auth", f"API key {redact(settings.api_key)}  [{source}]")

    try:
        from .azure import ENTRA_SCOPE, entra_token_provider

        provider = entra_token_provider(tenant=settings.tenant, subscription=settings.subscription)
        token = provider()
    except Exception as exc:  # credential chain failures are varied and noisy
        return Check(
            FAIL,
            "auth",
            f"Entra ID sign-in failed: {type(exc).__name__}",
            "Run 'az login', or set QQ_API_KEY to use an API key instead.",
        )
    if not token:
        return Check(FAIL, "auth", "Entra ID returned an empty token", "Run 'az login'.")
    where = f" in tenant {settings.tenant}" if settings.tenant else " (tenant: CLI default)"
    return Check(OK, "auth", f"Microsoft Entra ID token acquired for {ENTRA_SCOPE}{where}")


def _check_surface(settings) -> Check:
    surface = settings.effective_api
    if surface == "responses":
        return Check(
            WARN,
            "api surface",
            "responses (only direct model deployments support this)",
            "model-router speaks Chat Completions only; drop QQ_API=responses for the router.",
        )
    return Check(OK, "api surface", "chat completions")


def _check_call(settings) -> Check:
    from .client import build_backend
    from .errors import QQError

    backend = build_backend(settings)
    label = f"{backend.provider} call"
    try:
        answer = backend.ask("Reply with the single word: ok")
    except QQError as exc:
        return Check(FAIL, label, exc.message, exc.hint or "")
    except Exception as exc:  # pragma: no cover - defensive
        return Check(FAIL, label, f"{type(exc).__name__}: {exc}")
    return Check(
        OK,
        label,
        f"routed to {answer.model or '?'} in {answer.latency:.2f}s via {answer.deployment}",
    )


def run_doctor(verbose: bool = False) -> int:
    print(f"qq doctor   config file: {config_path()}")
    print()

    checks = [_check_install(), _check_dependencies()]
    for check in checks:
        _emit(check)
    if any(c.status == FAIL for c in checks):
        return EXIT_ERROR

    try:
        settings = resolve()
    except Exception as exc:
        _emit(Check(FAIL, "config", str(exc), "Fix or delete the config file."))
        return EXIT_ERROR

    staged = [
        _check_provider(settings),
        _check_endpoint(settings),
        _check_deployment(settings),
        _check_surface(settings),
    ]
    for check in staged:
        _emit(check)
        checks.append(check)
    if any(c.status == FAIL for c in staged):
        print("\nstopping early: fix the failures above, then run 'qq doctor' again")
        return EXIT_ERROR

    auth = _check_auth(settings)
    _emit(auth)
    checks.append(auth)
    if auth.status == FAIL:
        print("\nstopping early: fix the failures above, then run 'qq doctor' again")
        return EXIT_ERROR

    call = _check_call(settings)
    _emit(call)
    checks.append(call)

    failures = [c for c in checks if c.status == FAIL]
    warnings = [c for c in checks if c.status == WARN]
    print()
    if failures:
        print(f"{len(failures)} check(s) failed")
        return EXIT_ERROR
    print("all checks passed" + (f", {len(warnings)} warning(s)" if warnings else ""))
    return EXIT_OK
