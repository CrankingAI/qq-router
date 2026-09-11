"""Command-line entry point for qq.

Design notes:

* stdout carries the answer and nothing else, so ``qq ... | pbcopy`` and
  ``git diff | qq summarize | less`` behave. Every diagnostic, prompt and error
  goes to stderr.
* Heavy imports (openai, azure-identity) are deferred, so ``qq --help`` and
  ``qq --version`` stay in the low tens of milliseconds.
* ``config`` and ``doctor`` are recognised as subcommands only when the rest of
  the command line looks like a subcommand invocation. ``qq doctor`` runs the
  diagnostic; ``qq doctor who is the best one`` asks a question.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence

from . import __version__
from .errors import EXIT_INTERRUPT, EXIT_OK, EXIT_USAGE, QQError, UsageError
from .prompt import build_prompt, join_args, truncate_stdin

PROGRAM = "qq"

CONFIG_VERBS = ("show", "get", "set", "unset", "path", "list")

USAGE_EXAMPLES = """\
examples:
  qq how do I list all my github repos
  qq "explain EIP-3009 in two sentences"
  git diff | qq summarize this
  cat error.txt | qq explain this error
  qq --verbose what is a CNAME
  qq -vvv what is a CNAME          # full server-side timing breakdown

subcommands:
  qq config [show|set KEY VALUE|unset KEY|path]
  qq doctor
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Ask a quick LLM question from your terminal, routed by Azure Model Router.",
        epilog=USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=True,
    )
    parser.add_argument("words", nargs="*", help="the question; quoting is optional")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help=(
            "routing diagnostics on stderr, never stdout. Repeat for more: "
            "-v model and latency, -vv connection and request context, "
            "-vvv server-side timing breakdown"
        ),
    )
    parser.add_argument(
        "-m",
        "--model",
        metavar="MODEL",
        help="address a specific deployment instead of the router",
    )
    parser.add_argument("--endpoint", metavar="URL", help="override the configured endpoint")
    parser.add_argument("--deployment", metavar="NAME", help="override the router deployment name")
    parser.add_argument(
        "--auth",
        choices=("auto", "entra", "key"),
        help="force an authentication mode (default: auto)",
    )
    parser.add_argument(
        "--api",
        choices=("auto", "chat", "responses"),
        help=(
            "API surface (default: auto, which uses chat completions; "
            "model-router does not support the responses API)"
        ),
    )
    parser.add_argument(
        "--tenant", metavar="ID", help="Entra tenant id that owns the Foundry resource"
    )
    parser.add_argument("--timeout", type=float, metavar="SECONDS", help="request timeout")
    stream = parser.add_mutually_exclusive_group()
    stream.add_argument(
        "--stream",
        dest="stream",
        action="store_true",
        default=None,
        help="stream the answer (default when stdout is a terminal)",
    )
    stream.add_argument(
        "--no-stream",
        dest="stream",
        action="store_false",
        help="wait for the whole answer before printing",
    )
    parser.add_argument(
        "--ask",
        action="store_true",
        help="treat the words as a question even if they start with a subcommand name",
    )
    parser.add_argument("-V", "--version", action="version", version=f"{PROGRAM} {__version__}")
    return parser


def read_stdin(stdin: object | None = None) -> str | None:
    """Return piped stdin, or None when stdin is a terminal or unavailable.

    An interactive terminal must never be read here: doing so would make a bare
    ``qq`` hang waiting for input instead of printing usage.
    """
    stream = sys.stdin if stdin is None else stdin
    if stream is None:
        return None
    try:
        if stream.isatty():  # type: ignore[attr-defined]
            return None
    except (AttributeError, ValueError):
        return None
    try:
        data = stream.read()  # type: ignore[attr-defined]
    except (OSError, ValueError):
        return None
    if not data:
        return None
    return data if isinstance(data, str) else data.decode("utf-8", "replace")


def detect_subcommand(words: Sequence[str], forced_ask: bool) -> str | None:
    """Decide whether the words are a subcommand rather than a question."""
    if forced_ask or not words:
        return None
    head = words[0]
    rest = words[1:]
    if head == "doctor" and not rest:
        return "doctor"
    if head == "config" and (not rest or rest[0] in CONFIG_VERBS):
        return "config"
    return None


def _out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def _should_stream(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    if os.environ.get("QQ_NO_STREAM"):
        return False
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def run_query(args: argparse.Namespace, stdin_text: str | None) -> int:
    from .client import FoundryBackend
    from .config import resolve

    typed = join_args(args.words)
    piped, truncated = truncate_stdin(stdin_text or "")
    if truncated and args.verbose:
        _err("[stdin truncated to the last 100000 characters]\n")

    prompt = build_prompt(typed, piped)
    if not prompt:
        raise UsageError(
            "no question given",
            hint="Try: qq how do I list all my github repos",
        )

    settings = resolve(
        model=args.model,
        deployment=args.deployment,
        endpoint=args.endpoint,
        auth=args.auth,
        tenant=args.tenant,
        api=args.api,
        timeout=args.timeout,
    )
    backend = FoundryBackend(settings)

    streaming = _should_stream(args.stream)
    if streaming:
        answer = backend.ask(prompt, stream=True, on_delta=_out)
        if not answer.text.endswith("\n"):
            _out("\n")
    else:
        answer = backend.ask(prompt)
        _out(answer.text + "\n")

    if args.verbose:
        _err(answer.diagnostics(args.verbose) + "\n")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        subcommand = detect_subcommand(args.words, args.ask)
        if subcommand == "doctor":
            from .doctor import run_doctor

            return run_doctor(verbose=args.verbose)
        if subcommand == "config":
            from .configcmd import run_config

            return run_config(args.words[1:])

        stdin_text = read_stdin()
        if not args.words and stdin_text is None:
            parser.print_help(sys.stderr)
            return EXIT_USAGE
        return run_query(args, stdin_text)
    except QQError as exc:
        _err(f"{PROGRAM}: {exc.message}\n")
        if exc.hint:
            _err(f"  hint: {exc.hint}\n")
        return exc.exit_code
    except KeyboardInterrupt:
        _err("\n")
        return EXIT_INTERRUPT
    except BrokenPipeError:
        # Downstream closed the pipe (`qq ... | head`). Exit quietly.
        with contextlib.suppress(Exception):
            sys.stdout.close()
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
