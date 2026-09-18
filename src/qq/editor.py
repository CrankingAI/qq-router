"""Compose a question in ``$EDITOR``.

The escape hatch for questions that do not fit on a command line: a pasted
stack trace, a paragraph of context, anything with quotes and dollar signs in
it. Nothing here passes through the shell, so the text is taken exactly as
typed.

The two template lines are stripped by exact match rather than by "starts with
``#``". qq exists partly because silent mangling is infuriating, and a question
that opens with ``#!/bin/sh`` or ``# heading`` deserves to survive.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .errors import ConfigError, UsageError

# nano before vi: someone who has not set $EDITOR is also someone who should not
# have to discover ``:wq`` to ask a question.
FALLBACK_EDITORS = ("nano", "vi")

TEMPLATE_LINES = (
    "# Type your question below, then save and quit. An empty file cancels.",
    "# These two lines are removed. Any other line starting with # is kept.",
)

TEMPLATE = "".join(line + "\n" for line in TEMPLATE_LINES)


def editor_command() -> list[str]:
    """The editor to launch, as an argv list.

    ``$VISUAL``/``$EDITOR`` may carry arguments (``code -w``, ``subl -w``), so
    the value is split the way a shell would split it.
    """
    for name in ("QQ_EDITOR", "VISUAL", "EDITOR"):
        raw = os.environ.get(name, "").strip()
        if raw:
            parts = shlex.split(raw)
            if parts:
                return parts
    for candidate in FALLBACK_EDITORS:
        if shutil.which(candidate):
            return [candidate]
    return ["vi"]


def strip_template(text: str) -> str:
    """Drop the seeded instruction lines, leaving everything else untouched."""
    kept = [line for line in text.splitlines() if line.rstrip() not in TEMPLATE_LINES]
    return "\n".join(kept).strip()


def _isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return False


def _launch(argv: list[str]) -> None:
    """Run the editor, giving it the terminal even when qq's own streams are not.

    ``git diff | qq -e explain this`` and ``qq -e ... > answer.txt`` both leave
    qq with a redirected stream that a full-screen editor cannot use, so the
    editor is pointed at /dev/tty for whichever side is not a terminal.
    """
    tty = None
    need_tty = not _isatty(sys.stdin) or not _isatty(sys.stdout)
    if need_tty:
        try:
            tty = open("/dev/tty", "r+")  # noqa: SIM115 - closed in finally
        except OSError as exc:
            raise UsageError(
                "--editor needs a terminal",
                hint="No /dev/tty here. Pipe the question in instead: echo '...' | qq",
            ) from exc

    stdin = tty if (tty and not _isatty(sys.stdin)) else None
    stdout = tty if (tty and not _isatty(sys.stdout)) else None
    try:
        completed = subprocess.run(argv, stdin=stdin, stdout=stdout, check=False)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"editor not found: {argv[0]}",
            hint="Set $EDITOR (or $QQ_EDITOR) to an editor on your PATH.",
        ) from exc
    finally:
        if tty is not None:
            tty.close()

    if completed.returncode != 0:
        raise UsageError(
            f"{argv[0]} exited with status {completed.returncode}",
            hint="Nothing was asked.",
        )


def compose(seed: str = "") -> str:
    """Open an editor and return what was typed, or "" if nothing was."""
    argv = editor_command()
    with tempfile.TemporaryDirectory(prefix="qq-") as tmp:
        path = Path(tmp) / "qq-question.md"
        path.write_text(TEMPLATE + (seed + "\n" if seed else ""), encoding="utf-8")
        _launch([*argv, str(path)])
        text = path.read_text(encoding="utf-8")
    return strip_template(text)
