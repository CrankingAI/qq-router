"""A small on-disk cache for Entra ID bearer tokens.

Every ``qq`` invocation is a fresh process, and ``AzureCliCredential`` shells
out to ``az`` to get a token, which costs roughly 0.65s. That is a large share
of a two-second question, so the token is cached until shortly before it
expires.

This mirrors what the Azure CLI itself already does: it keeps an MSAL token
cache under ``~/.azure``. The file here is written with ``0600`` inside the
``0700`` config directory, holds nothing but the token and its expiry, and can
be disabled entirely with ``QQ_NO_TOKEN_CACHE=1``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .config import config_dir

#: Treat a token as expired this many seconds early, so a request never starts
#: with a token that dies mid-flight.
REFRESH_MARGIN_SECONDS = 300

CACHE_FILENAME = "token-cache.json"


def disabled() -> bool:
    return os.environ.get("QQ_NO_TOKEN_CACHE", "").strip().lower() in ("1", "true", "yes", "on")


def cache_path() -> Path:
    return config_dir() / CACHE_FILENAME


def cache_key(tenant: str | None, scope: str) -> str:
    """Derive an opaque key. The tenant is not a secret, but hashing keeps the
    file uniform and avoids leaking the tenant layout to anything that lists it."""
    raw = f"{tenant or ''}|{scope}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _read_all() -> dict[str, dict]:
    path = cache_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(key: str, now: float | None = None) -> str | None:
    """Return a still-valid cached token, or None."""
    if disabled():
        return None
    entry = _read_all().get(key)
    if not isinstance(entry, dict):
        return None
    token = entry.get("token")
    expires_on = entry.get("expires_on")
    if not isinstance(token, str) or not isinstance(expires_on, (int, float)):
        return None
    current = time.time() if now is None else now
    if expires_on - REFRESH_MARGIN_SECONDS <= current:
        return None
    return token


def store(key: str, token: str, expires_on: float) -> None:
    """Persist a token. Failures are swallowed: a cache miss is not an error."""
    if disabled():
        return
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            path.parent.chmod(stat.S_IRWXU)

        data = _read_all()
        data[key] = {"token": token, "expires_on": float(expires_on)}
        # Drop anything already expired so the file cannot grow without bound.
        now = time.time()
        data = {
            k: v
            for k, v in data.items()
            if isinstance(v, dict) and float(v.get("expires_on", 0)) > now
        }

        tmp = path.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        return


def clear() -> None:
    """Delete the cache file."""
    with contextlib.suppress(OSError):
        cache_path().unlink(missing_ok=True)
