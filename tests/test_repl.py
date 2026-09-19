"""The qq> prompt.

The invariant these protect, same as the rest of the CLI: stdout carries
answers and nothing else. The banner, the prompt, the help and every error are
stderr, so ``qq > answers.txt`` with no arguments produces a file of answers.
"""

import io

import pytest

from qq import __version__
from qq.errors import EXIT_CONFIG, EXIT_OK, AuthError, ConfigError, QQError
from qq.repl import run_repl


class Recorder:
    """Stands in for the backend: records questions, optionally raises."""

    def __init__(self, raises=None):
        self.asked = []
        self.raises = raises

    def __call__(self, question):
        self.asked.append(question)
        if self.raises is not None:
            raise self.raises


def drive(monkeypatch, typed, **kwargs):
    """Run the prompt against a scripted stdin and return (code, asks)."""
    monkeypatch.setattr("sys.stdin", io.StringIO(typed))
    ask = kwargs.pop("ask", None) or Recorder()
    code = run_repl(ask=ask, **kwargs)
    return code, ask


def test_eof_ends_the_session_cleanly(monkeypatch, capsys):
    code, ask = drive(monkeypatch, "")
    assert code == EXIT_OK
    assert ask.asked == []


def test_a_line_is_asked_verbatim(monkeypatch, capsys):
    """The whole point: no quoting, no globbing, no expansion."""
    code, ask = drive(monkeypatch, "what's the difference between $PATH and ~/bin?\n")
    assert code == EXIT_OK
    assert ask.asked == ["what's the difference between $PATH and ~/bin?"]


def test_each_line_is_a_separate_question(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "what is a CNAME\nwhat is an A record\n")
    assert ask.asked == ["what is a CNAME", "what is an A record"]


def test_blank_lines_are_ignored(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "\n   \nwhat is a CNAME\n")
    assert ask.asked == ["what is a CNAME"]


@pytest.mark.parametrize("word", ["/exit", "/quit", "/q", "exit", "quit", "EXIT"])
def test_exit_commands_quit_without_asking(monkeypatch, capsys, word):
    code, ask = drive(monkeypatch, f"{word}\nnever asked\n")
    assert code == EXIT_OK
    assert ask.asked == []


def test_help_goes_to_stderr_and_asks_nothing(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "/help\n")
    captured = capsys.readouterr()
    assert ask.asked == []
    assert captured.out == ""
    assert "/e" in captured.err


def test_banner_and_prompt_stay_off_stdout(monkeypatch, capsys):
    drive(monkeypatch, "what is a CNAME\n")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "qq> " in captured.err
    assert "Ctrl-D" in captured.err


def test_the_banner_names_the_running_version(monkeypatch, capsys):
    """Which qq is this: a session has no other header to say so.

    More than one qq can answer to ``qq`` - a uv tool, a pipx one, a venv on
    PATH - and the banner is where a session says which one it opened.
    """
    drive(monkeypatch, "what is a CNAME\n")
    captured = capsys.readouterr()
    assert f"qq {__version__}" in captured.err
    assert captured.out == ""


def test_a_trailing_backslash_continues_the_question(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "explain this traceback:\\\n  File x, line 1\n")
    assert ask.asked == ["explain this traceback:\n  File x, line 1"]


def test_a_continued_line_is_not_read_as_a_command(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "what does this mean:\\\n/exit\n")
    assert ask.asked == ["what does this mean:\n/exit"]


def test_an_initial_question_is_asked_before_the_prompt(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "", initial="what is a CNAME")
    assert ask.asked == ["what is a CNAME"]


def test_an_empty_initial_question_is_skipped(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "", initial="   ")
    assert ask.asked == []


def test_an_ordinary_failure_keeps_the_session_alive(monkeypatch, capsys):
    ask = Recorder(raises=AuthError("sign-in failed", hint="az login"))
    code, ask = drive(monkeypatch, "one\ntwo\n", ask=ask)
    assert code == EXIT_OK
    assert ask.asked == ["one", "two"]
    err = capsys.readouterr().err
    assert "sign-in failed" in err
    assert "hint: az login" in err


def test_a_config_failure_stops_the_session(monkeypatch, capsys):
    """It will not fix itself between prompts, so do not fail identically forever."""
    ask = Recorder(raises=ConfigError("no endpoint configured"))
    code, ask = drive(monkeypatch, "one\ntwo\n", ask=ask)
    assert code == EXIT_CONFIG
    assert ask.asked == ["one"]


def test_ctrl_c_while_answering_keeps_the_session(monkeypatch, capsys):
    ask = Recorder(raises=KeyboardInterrupt())
    code, ask = drive(monkeypatch, "one\ntwo\n", ask=ask)
    assert code == EXIT_OK
    assert ask.asked == ["one", "two"]


def test_the_editor_command_supplies_the_question(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "/e\n", compose=lambda seed: "a long question")
    assert ask.asked == ["a long question"]


def test_an_empty_editor_asks_nothing(monkeypatch, capsys):
    _code, ask = drive(monkeypatch, "/e\n", compose=lambda seed: "")
    assert ask.asked == []


def test_an_editor_failure_does_not_end_the_session(monkeypatch, capsys):
    def boom(seed):
        raise ConfigError("editor not found: nope")

    _code, ask = drive(monkeypatch, "/e\nwhat is a CNAME\n", compose=boom)
    assert ask.asked == ["what is a CNAME"]
    assert "editor not found" in capsys.readouterr().err


def test_a_failed_question_reports_its_diagnostics_under_verbose(monkeypatch, capsys):
    """-v means the same thing at the prompt as it does on the command line."""

    class _Answer:
        def diagnostics(self, level):
            return "[provider=azure deployment=qq-router]"

    ask = Recorder(raises=QQError("Azure returned an empty answer", answer=_Answer()))
    drive(monkeypatch, "did redsox win\n", ask=ask, verbose=1)
    err = capsys.readouterr().err
    assert "qq: Azure returned an empty answer" in err
    assert "[provider=azure deployment=qq-router]" in err


def test_a_failed_question_stays_quiet_without_verbose(monkeypatch, capsys):
    class _Answer:
        def diagnostics(self, level):  # pragma: no cover - must not be called
            raise AssertionError("diagnostics rendered without --verbose")

    ask = Recorder(raises=QQError("Azure returned an empty answer", answer=_Answer()))
    drive(monkeypatch, "did redsox win\n", ask=ask)
    assert "[" not in capsys.readouterr().err
