"""Error types for qq.

Every user-facing failure is a QQError with a short message and an exit code.
``hint`` carries the one-line "here's how to fix it" suggestion, which keeps the
remediation advice next to the failure instead of scattered through the CLI.
``answer`` carries what the request managed to learn before it failed, so
``--verbose`` has something to print on the way out.
"""

from __future__ import annotations

from typing import Any

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

    def __init__(self, message: str, *, hint: str | None = None, answer: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        #: The partial :class:`~qq.client.Answer` for the request that failed,
        #: when qq got far enough to have one. Typed loosely so this module
        #: stays free of imports, and set after the fact by the backend when
        #: an error travels up from deeper in the call.
        self.answer = answer

    def diagnostics(self, level: int = 1) -> str:
        """Routing details for the failed request, or "" when there are none.

        The empty-answer hint tells the user to re-run with --verbose. This is
        what makes that true: the same diagnostics a successful answer prints,
        for a question that did not produce one.
        """
        if level <= 0 or self.answer is None:
            return ""
        return self.answer.diagnostics(level)


class ConfigError(QQError):
    exit_code = EXIT_CONFIG


class AuthError(QQError):
    exit_code = EXIT_AUTH


class NetworkError(QQError):
    exit_code = EXIT_NETWORK


class RateLimitedError(NetworkError):
    """The provider is busy: the one failure worth trying the standby for.

    A distinct type rather than a string match on the message, because
    :class:`~qq.client.FailoverBackend` has to tell "wait and retry" apart
    from every other reason a request can fail.
    """


class UsageError(QQError):
    exit_code = EXIT_USAGE
