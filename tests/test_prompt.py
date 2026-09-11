"""Argument joining and stdin combination."""

from qq.prompt import MAX_STDIN_CHARS, build_prompt, join_args, truncate_stdin


def test_bare_words_join_into_one_prompt():
    assert join_args(["how", "do", "I", "list", "repos"]) == "how do I list repos"


def test_quoted_and_unquoted_forms_agree():
    unquoted = join_args(["explain", "EIP-3009", "in", "two", "sentences"])
    quoted = join_args(["explain EIP-3009 in two sentences"])
    assert unquoted == quoted


def test_join_drops_empty_arguments_and_trims():
    assert join_args(["", "hello", "", "world"]) == "hello world"
    assert join_args([]) == ""


def test_question_only():
    assert build_prompt("what is a CNAME", None) == "what is a CNAME"


def test_stdin_only_is_the_whole_question():
    assert build_prompt("", "what is EIP-3009?") == "what is EIP-3009?"


def test_question_and_stdin_are_clearly_separated():
    result = build_prompt("explain this error", "Traceback: boom")
    assert result.startswith("explain this error")
    assert "--- input ---" in result
    assert "Traceback: boom" in result
    assert result.index("explain this error") < result.index("Traceback: boom")


def test_empty_everything_is_empty():
    assert build_prompt("", None) == ""
    assert build_prompt("   ", "   ") == ""


def test_truncate_keeps_the_tail_because_errors_live_there():
    text = "".join(str(i % 10) for i in range(MAX_STDIN_CHARS + 500))
    clipped, was_truncated = truncate_stdin(text)
    assert was_truncated is True
    assert len(clipped) == MAX_STDIN_CHARS
    assert text.endswith(clipped)


def test_short_stdin_is_untouched():
    clipped, was_truncated = truncate_stdin("small")
    assert (clipped, was_truncated) == ("small", False)
