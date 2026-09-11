"""Turn argv and stdin into a single prompt string.

Kept free of I/O and of any Azure concern so the joining rules can be tested
directly, and so a different backend could reuse them unchanged.
"""

from __future__ import annotations

# Short, blunt, and aimed at a terminal. The goal is an answer that can be read
# in one glance and, when it is a command, pasted straight into the shell.
SYSTEM_INSTRUCTION = (
    "You answer questions typed into a terminal by an experienced software engineer.\n"
    "Rules:\n"
    "- Answer immediately. No preamble, no restating the question, no sign-off.\n"
    "- Be brief. Most answers are 1-5 sentences or a short command block.\n"
    "- Expand only when the question genuinely requires it. Never pad a simple\n"
    "  question into an essay.\n"
    "- When the answer is a command, put the exact command in a fenced code block\n"
    "  with a language tag, ready to copy and run. Prefer one canonical command\n"
    "  over a menu of options.\n"
    "- Use light Markdown (code spans, fenced blocks, short bullet lists). Do not\n"
    "  use headings.\n"
    "- If the question is ambiguous, answer the most likely reading and note the\n"
    "  assumption in one short line. Do not ask a clarifying question back.\n"
    "- If you do not know, say so in one line.\n"
)

# Ceiling on piped input. Guards against `cat huge.log | qq` turning into a
# surprise bill or a context-length error; the tail is kept because that is
# where errors live.
MAX_STDIN_CHARS = 100_000


def join_args(args: list[str]) -> str:
    """Join positional arguments into one prompt.

    ``qq how do I list my repos`` and ``qq "how do I list my repos"`` must
    produce the same prompt, so arguments are simply joined with single spaces.
    """
    return " ".join(a for a in args if a != "").strip()


def truncate_stdin(text: str, limit: int = MAX_STDIN_CHARS) -> tuple[str, bool]:
    """Clip overlong stdin, keeping the tail. Returns (text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def build_prompt(argv_text: str, stdin_text: str | None) -> str:
    """Combine the typed question with piped stdin.

    The three shapes a user can produce:

    * question only            -> the question
    * stdin only               -> the piped text, treated as the whole question
    * question and stdin       -> the question first, then the piped text in a
      fenced block labelled as input, so the model treats it as data rather
      than as instructions
    """
    question = argv_text.strip()
    piped = (stdin_text or "").strip()

    if question and piped:
        return f"{question}\n\n--- input ---\n{piped}\n--- end input ---"
    if piped:
        return piped
    return question
