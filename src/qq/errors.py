"""Error types for qq.

Every user-facing failure is a QQError with a short message and an exit code.
``hint`` carries the one-line "here's how to fix it" suggestion, which keeps the
remediation advice next to the failure instead of scattered through the CLI.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_NETWORK = 5
EXIT_INTERRUPT = 130


class QQError(Exception):
    """A failure that should be reported to the user without a traceback."""

    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(QQError):
    exit_code = EXIT_CONFIG


class AuthError(QQError):
    exit_code = EXIT_AUTH


class NetworkError(QQError):
    exit_code = EXIT_NETWORK


class UsageError(QQError):
    exit_code = EXIT_USAGE
