"""End-to-end CLI behaviour with the Azure backend stubbed out.

The invariant these tests exist to protect: stdout carries the answer and
nothing else, so qq composes in a pipeline.
"""

import io
import pathlib

import pytest

import qq.client as client_module
from qq import __version__
from qq.cli import build_parser, detect_subcommand, main, read_stdin
from qq.client import Answer
from qq.errors import EXIT_AUTH, EXIT_CONFIG, EXIT_OK, EXIT_USAGE, AuthError

SECRET = "azure-api-key-do-not-leak"
BRAVE_SECRET = "brave-api-key-do-not-leak"
PROJECT_ENDPOINT = "https://qq-dev-abc.services.ai.azure.com/api/projects/qq-dev"


class StubBackend:
    """Records the prompt it was asked and returns a canned answer."""

    last_prompt = None
    last_stream = None
    last_search = None
    raises = None

    def __init__(self, settings):
        self.settings = settings

    @property
    def target(self):
        return self.settings.model or self.settings.deployment

    @property
    def surface(self):
        return self.settings.effective_api

    def ask(self, prompt, *, stream=False, on_delta=None, search=None):
        StubBackend.last_prompt = prompt
        StubBackend.last_stream = stream
        StubBackend.last_search = search
        if StubBackend.raises is not None:
            raise StubBackend.raises
        text = "A CNAME record aliases one DNS name to another."
        if stream and on_delta:
            for piece in (text[:20], text[20:]):
                on_delta(piece)
        return Answer(
            text=text,
            model="gpt-5.6-luna",
            deployment=self.target,
            latency=0.71,
            input_tokens=9,
            output_tokens=14,
            provider="azure",
            router="model-router:2025-11-18",
            host="qq-dev-abc.openai.azure.com",
            api=self.settings.effective_api,
            auth=self.settings.effective_auth,
            stream=stream,
            request_id="chatcmpl-STUB",
            server_timings={"service_ttlt_ms": 300, "engine_ttft_ms": 90},
            replica="replica-7",
            cached_tokens=0,
            reasoning_tokens=0,
            token_cache="hit",
        )


@pytest.fixture(autouse=True)
def stub_backend(monkeypatch):
    StubBackend.last_prompt = None
    StubBackend.last_stream = None
    StubBackend.last_search = None
    StubBackend.raises = None
    monkeypatch.setattr(client_module, "build_backend", StubBackend)
    monkeypatch.setenv("QQ_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("QQ_API_KEY", SECRET)
    monkeypatch.setenv("QQ_DEPLOYMENT", "qq-router")
    monkeypatch.setenv("QQ_NO_STREAM", "1")
    yield


def run(argv, monkeypatch, capsys, stdin=None):
    monkeypatch.setattr("sys.stdin", stdin if stdin is not None else io.StringIO())
    if stdin is None:
        monkeypatch.setattr("qq.cli.read_stdin", lambda *a, **k: None)
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_bare_words_become_the_prompt(monkeypatch, capsys):
    code, out, err = run(["what", "is", "a", "CNAME"], monkeypatch, capsys)
    assert code == EXIT_OK
    assert StubBackend.last_prompt == "what is a CNAME"
    assert out.strip().startswith("A CNAME record")
    assert err == ""


def test_quoted_question_matches_unquoted(monkeypatch, capsys):
    run(["explain EIP-3009 in two sentences"], monkeypatch, capsys)
    first = StubBackend.last_prompt
    run(["explain", "EIP-3009", "in", "two", "sentences"], monkeypatch, capsys)
    assert StubBackend.last_prompt == first


def test_piped_stdin_alone_is_the_question(monkeypatch, capsys):
    code, _out, _ = run([], monkeypatch, capsys, stdin=io.StringIO("what is EIP-3009?"))
    assert code == EXIT_OK
    assert StubBackend.last_prompt == "what is EIP-3009?"


def test_piped_stdin_combines_with_the_typed_question(monkeypatch, capsys):
    run(["explain", "this", "error"], monkeypatch, capsys, stdin=io.StringIO("Traceback: boom"))
    assert StubBackend.last_prompt.startswith("explain this error")
    assert "Traceback: boom" in StubBackend.last_prompt


def test_verbose_diagnostics_go_to_stderr_only(monkeypatch, capsys):
    code, out, err = run(["--verbose", "what", "is", "a", "CNAME"], monkeypatch, capsys)
    assert code == EXIT_OK
    assert "model=gpt-5.6-luna" in err
    assert "latency=0.71s" in err
    assert "qq-router" in err
    assert "model=" not in out
    assert "latency" not in out


def test_verbose_output_never_contains_the_api_key(monkeypatch, capsys):
    _, out, err = run(["--verbose", "hello"], monkeypatch, capsys)
    assert SECRET not in out
    assert SECRET not in err


def test_error_output_never_contains_the_api_key(monkeypatch, capsys):
    StubBackend.raises = AuthError("rejected", hint="check QQ_API_KEY")
    code, out, err = run(["--verbose", "hello"], monkeypatch, capsys)
    assert code == EXIT_AUTH
    assert SECRET not in out
    assert SECRET not in err
    assert "rejected" in err
    assert out == ""


def test_azure_errors_exit_nonzero_with_a_hint(monkeypatch, capsys):
    StubBackend.raises = AuthError("Azure rejected the credentials (401)", hint="run az login")
    code, _out, err = run(["hello"], monkeypatch, capsys)
    assert code == EXIT_AUTH
    assert "hint: run az login" in err


def test_model_flag_addresses_a_specific_deployment(monkeypatch, capsys):
    code, _, err = run(["--model", "gpt-5.6-sol", "--verbose", "hi"], monkeypatch, capsys)
    assert code == EXIT_OK
    assert "deployment=gpt-5.6-sol" in err


def test_no_arguments_and_no_stdin_prints_usage_to_stderr(monkeypatch, capsys):
    code, out, err = run([], monkeypatch, capsys)
    assert code == EXIT_USAGE
    assert out == ""
    assert "usage:" in err


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag(capsys, flag):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args([flag])
    assert excinfo.value.code == 0
    # On stdout, and the whole of it: `qq --version` is what a bug report and a
    # `qq --version | cut -d' ' -f2` both read.
    assert capsys.readouterr().out.strip() == f"qq {__version__}"


def test_the_packaged_version_is_read_from_the_source_line():
    """pyproject declares the version dynamic and points hatch at this line.

    The wiring is the part that can drift silently: reformat the assignment and
    the build still succeeds, carrying whatever version hatch last managed to
    parse. Nothing else in the suite would notice, so this does.
    """
    import re
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert "version" in pyproject["project"]["dynamic"]

    source = root / pyproject["tool"]["hatch"]["version"]["path"]
    # hatch's default pattern for a version file.
    found = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", source.read_text(), re.MULTILINE)
    assert found and found.group(1) == __version__


def test_streaming_writes_the_answer_to_stdout(monkeypatch, capsys):
    code, out, err = run(["--stream", "what", "is", "a", "CNAME"], monkeypatch, capsys)
    assert code == EXIT_OK
    assert StubBackend.last_stream is True
    assert out.strip().startswith("A CNAME record")
    assert err == ""


def test_no_stream_flag_disables_streaming(monkeypatch, capsys):
    run(["--no-stream", "hi"], monkeypatch, capsys)
    assert StubBackend.last_stream is False


def test_read_stdin_returns_none_for_a_terminal():
    class Tty(io.StringIO):
        def isatty(self):
            return True

    assert read_stdin(Tty("ignored")) is None


def test_read_stdin_returns_piped_text():
    assert read_stdin(io.StringIO("piped")) == "piped"


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["doctor"], "doctor"),
        (["config"], "config"),
        (["config", "show"], "config"),
        (["config", "set", "endpoint", "https://x"], "config"),
        (["doctor", "who", "is", "the", "best"], None),
        (["config", "a", "load", "balancer"], None),
        (["what", "is", "a", "CNAME"], None),
        ([], None),
    ],
)
def test_subcommand_detection_does_not_hijack_questions(words, expected):
    assert detect_subcommand(words, forced_ask=False) == expected


def test_ask_flag_forces_a_question(monkeypatch, capsys):
    assert detect_subcommand(["doctor"], forced_ask=True) is None
    code, _, _ = run(["--ask", "doctor"], monkeypatch, capsys)
    assert code == EXIT_OK
    assert StubBackend.last_prompt == "doctor"


def test_api_flag_selects_the_responses_surface(monkeypatch, capsys):
    """On an account endpoint auto means chat; --api responses overrides it."""
    code, _, _ = run(["--api", "responses", "hi"], monkeypatch, capsys)
    assert code == EXIT_OK


def test_default_surface_is_chat_completions_on_the_account_endpoint(monkeypatch, capsys):
    from qq.config import resolve

    monkeypatch.delenv("QQ_API", raising=False)
    assert resolve().effective_api == "chat"


def test_default_surface_is_responses_on_a_project_endpoint(monkeypatch, capsys):
    from qq.config import resolve

    monkeypatch.delenv("QQ_API", raising=False)
    monkeypatch.setenv("QQ_ENDPOINT", PROJECT_ENDPOINT)
    assert resolve().effective_api == "responses"


def test_repeated_v_raises_the_tier(monkeypatch, capsys):
    _, _out, err1 = run(["-v", "hi"], monkeypatch, capsys)
    _, _out, err2 = run(["-vv", "hi"], monkeypatch, capsys)
    _, _out, err3 = run(["-vvv", "hi"], monkeypatch, capsys)
    assert len(err1.strip().splitlines()) == 1
    assert len(err2.strip().splitlines()) == 2
    assert len(err3.strip().splitlines()) == 4


def test_higher_tiers_keep_the_original_line_first(monkeypatch, capsys):
    _, _out, err1 = run(["-v", "hi"], monkeypatch, capsys)
    _, _out, err3 = run(["-vvv", "hi"], monkeypatch, capsys)
    assert err3.splitlines()[0] == err1.splitlines()[0]


def test_every_tier_stays_off_stdout(monkeypatch, capsys):
    """The whole point of stderr: qq must still compose in a pipeline."""
    for flag in ("-v", "-vv", "-vvv"):
        _, out, err = run([flag, "hi"], monkeypatch, capsys)
        assert out.strip() == "A CNAME record aliases one DNS name to another."
        assert "[" not in out
        assert "router=" not in out
        assert "server " not in out
        assert "router=model-router:2025-11-18" in err or flag == "-v"


def test_no_tier_leaks_the_api_key_to_either_stream(monkeypatch, capsys):
    for flag in ("-v", "-vv", "-vvv"):
        _, out, err = run([flag, "hi"], monkeypatch, capsys)
        assert SECRET not in out
        assert SECRET not in err


def test_verbose_defaults_to_off(monkeypatch, capsys):
    _, out, err = run(["hi"], monkeypatch, capsys)
    assert err == ""
    assert out.strip()


def test_the_v_line_names_the_backend(monkeypatch, capsys):
    _, _out, err = run(["-v", "hi"], monkeypatch, capsys)
    assert err.splitlines()[0].startswith("[provider=azure ")


# --- --search ----------------------------------------------------------------


def test_search_is_off_unless_asked_for(monkeypatch, capsys):
    run(["hi"], monkeypatch, capsys)
    assert StubBackend.last_search is None


def test_search_flag_reaches_the_backend_as_a_tool(monkeypatch, capsys):
    from qq.search import WebSearch

    monkeypatch.setenv("QQ_ENDPOINT", PROJECT_ENDPOINT)
    monkeypatch.setenv("QQ_BRAVE_API_KEY", BRAVE_SECRET)
    code, out, err = run(["--search", "newest", "python"], monkeypatch, capsys)
    assert code == EXIT_OK
    assert isinstance(StubBackend.last_search, WebSearch)
    assert err == ""
    assert BRAVE_SECRET not in out


def test_search_from_the_environment_can_be_switched_off_per_call(monkeypatch, capsys):
    monkeypatch.setenv("QQ_ENDPOINT", PROJECT_ENDPOINT)
    monkeypatch.setenv("QQ_BRAVE_API_KEY", BRAVE_SECRET)
    monkeypatch.setenv("QQ_SEARCH", "true")
    run(["hi"], monkeypatch, capsys)
    assert StubBackend.last_search is not None
    run(["--no-search", "hi"], monkeypatch, capsys)
    assert StubBackend.last_search is None


def test_search_without_a_brave_key_fails_before_any_request(monkeypatch, capsys):
    monkeypatch.setenv("QQ_ENDPOINT", PROJECT_ENDPOINT)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("QQ_BRAVE_API_KEY", raising=False)
    code, out, err = run(["--search", "hi"], monkeypatch, capsys)
    assert code == EXIT_CONFIG
    assert out == ""
    assert "brave_api_key" in err
    assert StubBackend.last_prompt is None


def test_search_on_the_account_endpoint_explains_the_project_endpoint(monkeypatch, capsys):
    """model-router only takes tools through a project endpoint; say so."""
    monkeypatch.setenv("QQ_BRAVE_API_KEY", BRAVE_SECRET)
    code, _out, err = run(["--search", "hi"], monkeypatch, capsys)
    assert code == EXIT_CONFIG
    assert "api/projects" in err
    assert BRAVE_SECRET not in err


def test_search_with_chat_forced_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("QQ_ENDPOINT", PROJECT_ENDPOINT)
    monkeypatch.setenv("QQ_BRAVE_API_KEY", BRAVE_SECRET)
    code, _out, err = run(["--search", "--api", "chat", "hi"], monkeypatch, capsys)
    assert code == EXIT_CONFIG
    assert "Responses" in err


def test_search_diagnostics_never_leak_the_brave_key(monkeypatch, capsys):
    monkeypatch.setenv("QQ_ENDPOINT", PROJECT_ENDPOINT)
    monkeypatch.setenv("QQ_BRAVE_API_KEY", BRAVE_SECRET)
    for flag in ("-v", "-vv", "-vvv"):
        _, out, err = run([flag, "--search", "hi"], monkeypatch, capsys)
        assert BRAVE_SECRET not in out
        assert BRAVE_SECRET not in err


# --- how a question gets in -------------------------------------------------
#
# The shell reads a command line before qq does, so unquoted English is mangled
# before argv exists. These cover the two ways in that the shell never touches.


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_a_bare_qq_at_a_terminal_opens_the_prompt(monkeypatch, capsys):
    """Not usage text: a person who typed `qq` wants to ask something."""
    monkeypatch.setattr("sys.stdin", Tty(""))
    code = main([])
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "qq> " in captured.err
    assert captured.out == ""


def test_a_bare_qq_without_a_terminal_still_prints_usage(monkeypatch, capsys):
    """`qq < /dev/null` in a script is a genuine 'I do not know what you want'."""
    code, out, err = run([], monkeypatch, capsys)
    assert code == EXIT_USAGE
    assert out == ""
    assert "usage:" in err


def test_the_prompt_asks_what_is_typed_at_it(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", Tty("what's a CNAME? $HOME (really)\n"))
    code = main([])
    assert code == EXIT_OK
    assert StubBackend.last_prompt == "what's a CNAME? $HOME (really)"


def test_interactive_flag_opens_the_prompt(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", Tty(""))
    code = main(["--interactive"])
    assert code == EXIT_OK
    assert "qq> " in capsys.readouterr().err


def test_interactive_with_words_asks_them_first(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", Tty(""))
    code = main(["-i", "what", "is", "a", "CNAME"])
    assert code == EXIT_OK
    assert StubBackend.last_prompt == "what is a CNAME"


def test_interactive_refuses_piped_input(monkeypatch, capsys):
    code, out, err = run(["-i"], monkeypatch, capsys, stdin=io.StringIO("piped"))
    assert code == EXIT_USAGE
    assert out == ""
    assert "cannot be combined with piped input" in err


def test_interactive_without_a_terminal_is_a_usage_error(monkeypatch, capsys):
    code, _out, err = run(["-i"], monkeypatch, capsys)
    assert code == EXIT_USAGE
    assert "needs a terminal" in err


def test_interactive_and_editor_together_are_refused(monkeypatch, capsys):
    code, _out, err = run(["-i", "-e"], monkeypatch, capsys)
    assert code == EXIT_USAGE
    assert "/e opens the editor" in err


def test_editor_flag_asks_what_was_composed(monkeypatch, capsys):
    monkeypatch.setattr("qq.editor.compose", lambda seed="": "what's a CNAME? (really)")
    code, out, _err = run(["-e"], monkeypatch, capsys)
    assert code == EXIT_OK
    assert StubBackend.last_prompt == "what's a CNAME? (really)"
    assert out.strip().startswith("A CNAME record")


def test_editor_flag_combines_with_piped_input(monkeypatch, capsys):
    monkeypatch.setattr("qq.editor.compose", lambda seed="": "explain this")
    code, _out, _err = run(["-e"], monkeypatch, capsys, stdin=io.StringIO("a traceback"))
    assert code == EXIT_OK
    assert StubBackend.last_prompt.startswith("explain this")
    assert "a traceback" in StubBackend.last_prompt


def test_an_empty_editor_buffer_asks_nothing(monkeypatch, capsys):
    monkeypatch.setattr("qq.editor.compose", lambda seed="": "")
    code, out, err = run(["-e"], monkeypatch, capsys)
    assert code == EXIT_USAGE
    assert out == ""
    assert "no question given" in err


def test_a_dashed_word_suggests_the_double_dash(monkeypatch, capsys):
    """argparse's own message is true and useless; the fix is --."""
    monkeypatch.setattr("sys.stdin", io.StringIO())
    with pytest.raises(SystemExit) as excinfo:
        main(["what", "does", "-rf", "do"])
    err = capsys.readouterr().err
    assert excinfo.value.code == EXIT_USAGE
    assert "hint:" in err
    assert "qq -- <question>" in err


def test_the_double_dash_lets_a_dashed_question_through(monkeypatch, capsys):
    code, _out, _err = run(["--", "what", "does", "-rf", "do"], monkeypatch, capsys)
    assert code == EXIT_OK
    assert StubBackend.last_prompt == "what does -rf do"


def test_an_ordinary_usage_error_gets_no_double_dash_hint(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        main(["--provider", "nonsense"])
    assert "qq -- <question>" not in capsys.readouterr().err


def test_the_help_text_names_every_way_in(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    text = capsys.readouterr().out
    assert "qq> prompt" in text
    assert "noglob qq" in text
    assert "-e" in text and "$EDITOR" in text
