"""Composing a question in $EDITOR.

The rule being protected: what comes back is what was typed. qq exists in part
because the shell silently mangles questions, so the editor path must not
invent a mangling of its own.
"""

import stat

import pytest

from qq.editor import TEMPLATE_LINES, compose, editor_command, strip_template
from qq.errors import ConfigError, UsageError


@pytest.fixture(autouse=True)
def no_editor_env(monkeypatch):
    for name in ("QQ_EDITOR", "VISUAL", "EDITOR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def pretend_streams_are_terminals(monkeypatch):
    """Skip the /dev/tty handoff, which is not available under pytest or in CI."""
    monkeypatch.setattr("qq.editor._isatty", lambda stream: True)


def fake_editor(tmp_path, script):
    """An 'editor' that runs `script` against the file it is handed."""
    path = tmp_path / "fake-editor"
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_the_seeded_lines_are_removed():
    text = "\n".join([*TEMPLATE_LINES, "what is a CNAME"])
    assert strip_template(text) == "what is a CNAME"


def test_other_comment_lines_survive():
    """A question may legitimately start with #; only the seeded lines go."""
    text = "\n".join([TEMPLATE_LINES[0], "what does #!/bin/sh do", "# and this heading"])
    assert strip_template(text) == "what does #!/bin/sh do\n# and this heading"


def test_dollars_and_quotes_survive():
    assert strip_template("what's the difference between $PATH and `pwd`?") == (
        "what's the difference between $PATH and `pwd`?"
    )


def test_an_untouched_template_yields_nothing():
    assert strip_template("\n".join(TEMPLATE_LINES)) == ""


@pytest.mark.parametrize("name", ["QQ_EDITOR", "VISUAL", "EDITOR"])
def test_the_editor_comes_from_the_environment(monkeypatch, name):
    monkeypatch.setenv(name, "myeditor")
    assert editor_command() == ["myeditor"]


def test_qq_editor_wins_over_editor(monkeypatch):
    monkeypatch.setenv("EDITOR", "vi")
    monkeypatch.setenv("QQ_EDITOR", "code -w")
    assert editor_command() == ["code", "-w"]


def test_editor_arguments_are_split_like_a_shell(monkeypatch):
    monkeypatch.setenv("EDITOR", "code --wait")
    assert editor_command() == ["code", "--wait"]


def test_a_blank_editor_variable_is_ignored(monkeypatch):
    monkeypatch.setenv("EDITOR", "   ")
    assert editor_command()[0] in ("nano", "vi")


def test_the_fallback_does_not_strand_you_in_vi(monkeypatch):
    """Someone who never set $EDITOR should not have to know :wq to ask a question."""
    monkeypatch.setattr(
        "qq.editor.shutil.which", lambda name: "/usr/bin/nano" if name == "nano" else None
    )
    assert editor_command() == ["nano"]


def test_compose_returns_what_was_typed(monkeypatch, tmp_path):
    monkeypatch.setenv("QQ_EDITOR", fake_editor(tmp_path, 'echo "what\'s a CNAME?" >> "$1"'))
    assert compose() == "what's a CNAME?"


def test_compose_seeds_the_words_already_typed(monkeypatch, tmp_path):
    """`qq -e summarize this diff` opens with those words there to expand on."""
    seen = tmp_path / "seen.txt"
    monkeypatch.setenv("QQ_EDITOR", fake_editor(tmp_path, f'cat "$1" > "{seen}"'))
    assert compose("summarize this diff") == "summarize this diff"
    buffer = seen.read_text()
    assert buffer.startswith(TEMPLATE_LINES[0])
    assert "summarize this diff" in buffer


def test_compose_returns_empty_when_nothing_is_typed(monkeypatch, tmp_path):
    monkeypatch.setenv("QQ_EDITOR", fake_editor(tmp_path, "true"))
    assert compose() == ""


def test_a_missing_editor_is_a_config_error(monkeypatch):
    monkeypatch.setenv("QQ_EDITOR", "definitely-not-an-editor-on-this-box")
    with pytest.raises(ConfigError) as excinfo:
        compose()
    assert "editor not found" in excinfo.value.message
    assert "$EDITOR" in (excinfo.value.hint or "")


def test_an_editor_that_fails_asks_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("QQ_EDITOR", fake_editor(tmp_path, "exit 1"))
    with pytest.raises(UsageError) as excinfo:
        compose()
    assert "status 1" in excinfo.value.message
