"""End-to-end CLI behaviour with the Azure backend stubbed out.

The invariant these tests exist to protect: stdout carries the answer and
nothing else, so qq composes in a pipeline.
"""

import io

import pytest

import qq.client as client_module
from qq.cli import build_parser, detect_subcommand, main, read_stdin
from qq.client import Answer
from qq.errors import EXIT_AUTH, EXIT_OK, EXIT_USAGE, AuthError

SECRET = "azure-api-key-do-not-leak"


class StubBackend:
    """Records the prompt it was asked and returns a canned answer."""

    last_prompt = None
    last_stream = None
    raises = None

    def __init__(self, settings):
        self.settings = settings

    @property
    def target(self):
        return self.settings.model or self.settings.deployment

    @property
    def surface(self):
        return self.settings.effective_api

    def ask(self, prompt, *, stream=False, on_delta=None):
        StubBackend.last_prompt = prompt
        StubBackend.last_stream = stream
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


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0
    assert "qq" in capsys.readouterr().out


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
    """model-router needs chat completions; --api responses is the opt-out."""
    code, _, _ = run(["--api", "responses", "hi"], monkeypatch, capsys)
    assert code == EXIT_OK


def test_default_surface_is_chat_completions(monkeypatch, capsys):
    from qq.config import resolve

    monkeypatch.delenv("QQ_API", raising=False)
    assert resolve().effective_api == "chat"


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
