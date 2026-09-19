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
* There are three ways in, because the shell mangles unquoted English before qq
  ever sees it: words on the command line, a ``qq>`` prompt (a bare ``qq`` at a
  terminal, or ``-i``), and ``$EDITOR`` (``-e``). Only the first is subject to
  quoting, globbing and expansion.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence

from . import __version__
from .errors import EXIT_INTERRUPT, EXIT_OK, EXIT_USAGE, ConfigError, QQError, UsageError
from .prompt import build_prompt, join_args, truncate_stdin

PROGRAM = "qq"

CONFIG_VERBS = ("show", "get", "set", "unset", "path", "list")

USAGE_EXAMPLES = """\
examples:
  qq how do I list all my github repos
  qq explain EIP-3009 in two sentences
  git diff | qq summarize this
  cat error.txt | qq explain this error
  qq --verbose what is a CNAME
  qq -vvv what is a CNAME           # full server-side timing breakdown
  qq --search what is the newest stable Python release
  qq -- what does -rf do            # -- when a word starts with a dash

ways to type a question:
  qq                                # a qq> prompt: no quoting, no globbing
  qq -i                             # the same prompt, asked for explicitly
  qq -e                             # compose it in $EDITOR, for long questions

  The shell reads a command line before qq does, so ? * ( ) ' $ # and friends
  need quoting there. At the qq> prompt and in the editor they do not. In zsh,
  `alias qq="noglob qq"` removes most of the need to quote.

subcommands:
  qq config [show|set KEY VALUE|unset KEY|path]
  qq doctor
"""

#: The last line of --help, where the version is both out of the way and still
#: on screen once the options have scrolled past. It answers "which qq is this,
#: and where does it live?" in one line - useful when several are installed, a
#: uv tool and a venv on PATH both answering to ``qq``. ``qq --version`` stays
#: the terse machine-readable form.
FOOTER = f"\n{PROGRAM} {__version__} - https://github.com/CrankingAI/qq-router\n"


def option_like_hint(message: str) -> str | None:
    """The one hint argparse cannot give by itself.

    ``qq what does -rf do`` fails with "unrecognized arguments: -rf do", which
    is true and useless. The fix is ``--``, and a question is far likelier than
    a typo'd flag.
    """
    if "unrecognized arguments" not in message:
        return None
    return (
        "a word in the question starts with '-', so qq read it as an option. "
        "Put -- first: qq -- <question>. Or type it at the qq> prompt: qq -i."
    )


class _Parser(argparse.ArgumentParser):
    """argparse, with qq's hint convention on usage errors."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        _err(f"{self.prog}: error: {message}\n")
        hint = option_like_hint(message)
        if hint:
            _err(f"  hint: {hint}\n")
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROGRAM,
        description="Ask a quick LLM question from your terminal, routed by Azure Model Router.",
        epilog=USAGE_EXAMPLES + FOOTER,
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
        "--provider",
        choices=("azure", "openrouter"),
        help="which backend to ask (default: azure)",
    )
    parser.add_argument(
        "--cost-tier",
        dest="cost_tier",
        choices=("low", "medium", "high", "xhigh", "max"),
        help="OpenRouter cost tier; the rough analogue of Azure's routing mode",
    )
    parser.add_argument(
        "--api",
        choices=("auto", "chat", "responses"),
        help=(
            "API surface (default: auto: responses on OpenRouter and on a Foundry "
            "project endpoint, chat completions for model-router on an account endpoint)"
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
    search = parser.add_mutually_exclusive_group()
    search.add_argument(
        "--search",
        dest="search",
        action="store_true",
        default=None,
        help=(
            "let the model search the web (Brave) when it thinks the answer may have "
            "changed since its training data; needs the Responses API and a Brave key"
        ),
    )
    search.add_argument(
        "--no-search",
        dest="search",
        action="store_false",
        help="never search, even if the config file says so",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "type questions at a qq> prompt, where shell quoting, globbing and "
            "$expansion do not apply. A bare qq at a terminal does the same"
        ),
    )
    parser.add_argument(
        "-e",
        "--editor",
        action="store_true",
        help="compose the question in $EDITOR, for long or multi-line questions",
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

    An interactive terminal must never be read here. A bare ``qq`` at a terminal
    opens the prompt in :mod:`qq.repl`, which reads a line at a time; slurping
    the terminal to EOF instead would look exactly like ``cat`` with no
    arguments, which is to say like a hang.
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


def stdin_is_tty(stdin: object | None = None) -> bool:
    """Whether a human is at the other end of stdin, rather than a pipe."""
    stream = sys.stdin if stdin is None else stdin
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return False


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


def _web_search(settings):
    """Build the search tool, or say precisely why it cannot be used."""
    from .search import WebSearch

    if settings.effective_api != "responses":
        if settings.effective_provider == "azure" and not settings.is_project_endpoint:
            hint = (
                "model-router only accepts the Responses API through a Foundry project "
                "endpoint. Re-run ./scripts/setup-cli.sh, or set the endpoint to "
                "https://<account>.services.ai.azure.com/api/projects/<project>."
            )
        else:
            hint = "Drop '--api chat' (or QQ_API=chat); search needs the Responses API."
        raise ConfigError("--search needs the Responses API", hint=hint)
    return WebSearch(settings.brave_api_key)


def _prepare(args: argparse.Namespace):
    """Resolve settings and build the backend and search tool.

    Split out of :func:`run_query` so the prompt can build them once and reuse
    them across a whole session: the backend holds no per-question state, and
    the ``openai`` import behind it costs about a second.
    """
    from .client import build_backend
    from .config import resolve, standby

    # One mapping, used twice: the standby has to be resolved from the same
    # overrides, and has to be able to see which of them name a provider.
    overrides: dict[str, object] = {
        "model": args.model,
        "deployment": args.deployment,
        "endpoint": args.endpoint,
        "auth": args.auth,
        "tenant": args.tenant,
        "api": args.api,
        "provider": args.provider,
        "cost_tier": args.cost_tier,
        "search": args.search,
        "timeout": args.timeout,
    }
    settings = resolve(**overrides)  # type: ignore[arg-type]
    backend = build_backend(settings, standby(settings, overrides))
    web = _web_search(settings) if settings.search else None
    return backend, web


def _answer(prompt: str, args: argparse.Namespace, backend, web) -> None:
    """Ask one question and write the answer to stdout, diagnostics to stderr."""
    if _should_stream(args.stream):
        answer = backend.ask(prompt, stream=True, on_delta=_out, search=web)
        if not answer.text.endswith("\n"):
            _out("\n")
    else:
        answer = backend.ask(prompt, search=web)
        _out(answer.text + "\n")

    if args.verbose:
        _err(answer.diagnostics(args.verbose) + "\n")


def _typed_and_piped(args: argparse.Namespace, stdin_text: str | None) -> tuple[str, str]:
    typed = join_args(args.words)
    piped, truncated = truncate_stdin(stdin_text or "")
    if truncated and args.verbose:
        _err("[stdin truncated to the last 100000 characters]\n")
    return typed, piped


def run_query(args: argparse.Namespace, stdin_text: str | None) -> int:
    typed, piped = _typed_and_piped(args, stdin_text)

    prompt = build_prompt(typed, piped)
    if not prompt:
        raise UsageError(
            "no question given",
            hint="Try: qq how do I list all my github repos",
        )

    backend, web = _prepare(args)
    _answer(prompt, args, backend, web)
    return EXIT_OK


def run_editor(args: argparse.Namespace, stdin_text: str | None) -> int:
    """``qq -e``: compose the question in $EDITOR, then ask it."""
    from .editor import compose

    seed, piped = _typed_and_piped(args, stdin_text)
    typed = compose(seed)

    prompt = build_prompt(typed, piped)
    if not prompt:
        raise UsageError(
            "no question given",
            hint="The editor was closed without a question in it.",
        )

    backend, web = _prepare(args)
    _answer(prompt, args, backend, web)
    return EXIT_OK


def run_interactive(args: argparse.Namespace) -> int:
    """``qq -i``, and a bare ``qq`` at a terminal: the qq> prompt.

    The backend is built on the first question rather than up front, so the
    prompt appears immediately. A broken configuration still reports itself with
    the same message and exit code it would have given a one-shot ``qq``.
    """
    from .editor import compose
    from .repl import run_repl

    session: dict[str, object] = {}

    def ask(question: str) -> None:
        if "backend" not in session:
            session["backend"], session["web"] = _prepare(args)
        _answer(question, args, session["backend"], session["web"])

    return run_repl(ask=ask, compose=compose, initial=join_args(args.words), verbose=args.verbose)


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

        if args.interactive and args.editor:
            raise UsageError(
                "use -i or -e, not both",
                hint="At the qq> prompt, /e opens the editor for one question.",
            )

        stdin_text = read_stdin()

        if args.editor:
            return run_editor(args, stdin_text)

        if args.interactive:
            if stdin_text is not None:
                raise UsageError(
                    "-i cannot be combined with piped input",
                    hint="Ask the piped question directly: git diff | qq summarize this",
                )
            if not stdin_is_tty():
                raise UsageError(
                    "-i needs a terminal to read questions from",
                    hint="Without one, pass the question as arguments: qq <question>",
                )
            return run_interactive(args)

        if not args.words and stdin_text is None:
            # A terminal means a person, and a person who typed a bare `qq`
            # wants to ask something. Only a non-terminal stdin that produced
            # nothing is a genuine "I do not know what you want".
            if stdin_is_tty():
                return run_interactive(args)
            parser.print_help(sys.stderr)
            return EXIT_USAGE

        return run_query(args, stdin_text)
    except QQError as exc:
        _err(f"{PROGRAM}: {exc.message}\n")
        if exc.hint:
            _err(f"  hint: {exc.hint}\n")
        # A failure is exactly when the routing details are worth having, and
        # the hint on an empty answer sends the user here.
        detail = exc.diagnostics(args.verbose)
        if detail:
            _err(detail + "\n")
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
