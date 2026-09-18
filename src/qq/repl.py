"""The ``qq>`` prompt: ask questions without the shell in the way.

Why this exists. Everything typed after ``qq`` on a command line is read by the
shell before qq ever runs, and ordinary English is full of shell metacharacters.
In zsh, ``qq what is a CNAME?`` never reaches qq at all (``no matches found:
CNAME?``); ``qq what's the difference`` hangs on an unterminated quote;
``qq what does $PATH mean`` silently asks about the expansion rather than the
variable. No flag can undo that, because argv arrives already mangled. A prompt
can: a line typed here is taken verbatim.

Deliberately stateless. Each line is a fresh question with no conversation
history, which is why the banner says so out loud. qq is for questions that do
not deserve a browser tab; remembering turns would change both the character of
the tool and the size of its bill.

This module knows nothing about Azure, config or the backend. It is handed an
``ask`` callable and drives the terminal around it, so the loop can be tested
with a fake.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable

from . import __version__
from .errors import EXIT_OK, ConfigError, QQError

PROMPT = "qq> "
CONTINUATION = "... "

EXIT_COMMANDS = frozenset({"/exit", "/quit", "/q", "exit", "quit"})
HELP_COMMANDS = frozenset({"/help", "/h", "/?"})
EDITOR_COMMANDS = frozenset({"/e", "/editor"})

#: A session has no other header, so the banner is where it says which qq this
#: is. Useful when several are installed - a uv tool, a pipx one, and a venv on
#: PATH all answer to ``qq``.
BANNER = (
    f"qq {__version__}: type a question and press return. Shell quoting does not apply here.\n"
    "  Each line is a separate question. Ctrl-D to quit, /help for the rest.\n"
)

HELP = (
    "  /help          this\n"
    "  /e             compose a long or multi-line question in $EDITOR\n"
    "  /exit          quit (so does Ctrl-D)\n"
    "  \\ at line end  continue the question on the next line\n"
    "\n"
    "  Each line is answered on its own. qq does not remember the last one,\n"
    "  so a bare 'why?' will not work; ask the whole question again.\n"
)


def _err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def _stdout_is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _read_line(prompt: str) -> str:
    """Read one line, keeping the prompt off stdout when stdout is redirected.

    ``input(prompt)`` writes the prompt to stdout, which readline needs in order
    to redraw the line correctly during editing and history recall. That is the
    right trade when stdout is the terminal. When stdout is a file or a pipe
    (``qq > answers.txt``), the answer stream must stay clean, so the prompt
    goes to stderr and readline is told there is no prompt.
    """
    if _stdout_is_tty():
        return input(prompt)
    _err(prompt)
    return input()


def _enable_line_editing() -> None:
    """Arrow keys, Ctrl-A, and in-session history, when readline is available.

    History is memory-only on purpose: questions are often the most sensitive
    thing a person types all day, and a dotfile full of them is not something
    this tool should create behind their back.
    """
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401


def run_repl(
    *,
    ask: Callable[[str], None],
    compose: Callable[[str], str] | None = None,
    initial: str | None = None,
) -> int:
    """Drive the prompt until EOF.

    ``ask`` answers one question and raises QQError on failure. ``compose``
    opens an editor and returns the text, for ``/e``. ``initial`` is answered
    before the first prompt, which is what ``qq -i <question>`` does.
    """
    _enable_line_editing()
    _err(BANNER)

    pending: list[str] = []
    queued = initial.strip() if initial and initial.strip() else None

    while True:
        if queued is None:
            try:
                line = _read_line(CONTINUATION if pending else PROMPT)
            except EOFError:
                _err("\n")
                return EXIT_OK
            except KeyboardInterrupt:
                # Abandon the half-typed question, keep the session.
                _err("\n")
                pending.clear()
                continue

            if line.endswith("\\"):
                pending.append(line[:-1])
                continue

            if pending:
                pending.append(line)
                question = "\n".join(pending).strip()
                pending.clear()
            else:
                question = line.strip()
                command = question.lower()
                if not question:
                    continue
                if command in EXIT_COMMANDS:
                    return EXIT_OK
                if command in HELP_COMMANDS:
                    _err(HELP)
                    continue
                if command in EDITOR_COMMANDS:
                    if compose is None:
                        _err("qq: no editor available here\n")
                        continue
                    try:
                        question = compose("")
                    except QQError as exc:
                        _report(exc)
                        continue
                    if not question:
                        continue
        else:
            question, queued = queued, None

        try:
            ask(question)
        except KeyboardInterrupt:
            _err("\n")
        except ConfigError as exc:
            # Configuration will not fix itself between prompts, so stop rather
            # than fail identically on every question from here on.
            _report(exc)
            return exc.exit_code
        except QQError as exc:
            _report(exc)
        _err("\n")


def _report(exc: QQError) -> None:
    _err(f"qq: {exc.message}\n")
    if exc.hint:
        _err(f"  hint: {exc.hint}\n")
