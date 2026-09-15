"""Configuration resolution, endpoint normalization, and secret handling."""

import stat

import pytest

from qq.config import (
    DEFAULT_DEPLOYMENT,
    Settings,
    config_dir,
    normalize_endpoint,
    read_config_file,
    redact,
    resolve,
    write_config_file,
)
from qq.errors import ConfigError

RESOURCE = "https://qq-dev-abc123.openai.azure.com"


def test_flags_beat_environment_which_beats_file():
    settings = resolve(
        env={"QQ_DEPLOYMENT": "from-env", "QQ_ENDPOINT": RESOURCE},
        file_values={"deployment": "from-file", "model": "from-file-model"},
        deployment="from-flag",
    )
    assert settings.deployment == "from-flag"
    assert settings.sources["deployment"] == "flag"
    assert settings.model == "from-file-model"
    assert settings.sources["model"] == "config file"


def test_environment_beats_file():
    settings = resolve(env={"QQ_DEPLOYMENT": "from-env"}, file_values={"deployment": "from-file"})
    assert settings.deployment == "from-env"
    assert settings.sources["deployment"] == "env:QQ_DEPLOYMENT"


def test_falls_back_to_azure_openai_variables():
    settings = resolve(
        env={"AZURE_OPENAI_ENDPOINT": RESOURCE, "AZURE_OPENAI_API_KEY": "sk-secret"},
        file_values={},
    )
    assert settings.endpoint == RESOURCE
    assert settings.api_key == "sk-secret"
    assert settings.sources["api_key"] == "env:AZURE_OPENAI_API_KEY"


def test_qq_variables_win_over_azure_openai_variables():
    settings = resolve(
        env={"QQ_API_KEY": "preferred", "AZURE_OPENAI_API_KEY": "fallback"}, file_values={}
    )
    assert settings.api_key == "preferred"


def test_default_deployment_when_nothing_is_set():
    settings = resolve(env={}, file_values={})
    assert settings.deployment == DEFAULT_DEPLOYMENT
    assert settings.sources["deployment"] == "default"


@pytest.mark.parametrize(
    "raw",
    [
        "https://x.openai.azure.com",
        "https://x.openai.azure.com/",
        "https://x.openai.azure.com/openai",
        "https://x.openai.azure.com/openai/v1",
        "https://x.openai.azure.com/openai/v1/",
        "x.openai.azure.com",
    ],
)
def test_endpoint_normalizes_to_the_v1_base_url(raw):
    assert normalize_endpoint(raw) == "https://x.openai.azure.com/openai/v1"


def test_endpoint_accepts_the_services_ai_host():
    got = normalize_endpoint("https://x.services.ai.azure.com/")
    assert got == "https://x.services.ai.azure.com/openai/v1"


def test_empty_endpoint_stays_empty():
    assert normalize_endpoint("") == ""
    assert normalize_endpoint("   ") == ""


def test_missing_endpoint_raises_with_a_hint():
    with pytest.raises(ConfigError) as excinfo:
        Settings().require_endpoint()
    assert excinfo.value.hint


def test_auth_auto_prefers_a_key_when_one_exists():
    assert Settings(api_key="k").effective_auth == "key"
    assert Settings().effective_auth == "entra"


def test_auth_can_be_forced_to_entra_despite_a_key():
    assert Settings(api_key="k", auth="entra").effective_auth == "entra"


def test_invalid_auth_mode_is_rejected():
    with pytest.raises(ConfigError):
        resolve(env={"QQ_AUTH": "magic"}, file_values={})


def test_invalid_timeout_is_rejected():
    with pytest.raises(ConfigError):
        resolve(env={"QQ_TIMEOUT": "soon"}, file_values={})


def test_redact_never_reveals_the_secret_or_its_length():
    masked = redact("super-secret-key-abcdef")
    assert "super" not in masked
    assert "abcdef" not in masked
    assert len(masked) == len(redact("x"))
    assert redact(None) == "(not set)"


def test_config_file_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "qq"))
    path = write_config_file({"endpoint": RESOURCE, "deployment": "qq-router", "timeout": 30.0})
    assert read_config_file(path) == {
        "endpoint": RESOURCE,
        "deployment": "qq-router",
        "timeout": 30.0,
    }


def test_config_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "qq"))
    path = write_config_file({"api_key": "secret"})
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_config_values_with_quotes_survive(tmp_path, monkeypatch):
    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "qq"))
    tricky = 'has "quotes" and \\ backslash'
    path = write_config_file({"deployment": tricky})
    assert read_config_file(path)["deployment"] == tricky


def test_missing_config_file_is_not_an_error(tmp_path):
    assert read_config_file(tmp_path / "nope.toml") == {}


def test_config_dir_honours_the_override(tmp_path, monkeypatch):
    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "custom"))
    assert config_dir() == tmp_path / "custom"


def test_config_get_finds_a_provider_scoped_secret(tmp_path, monkeypatch):
    """The bug: 'qq config get openrouter_api_key' looked up a Settings
    attribute that does not exist and reported "(not set)" with a key stored."""
    from qq.configcmd import _KEY_TO_FIELD, _get

    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "qq"))
    monkeypatch.setenv("QQ_PROVIDER", "openrouter")
    monkeypatch.setenv("QQ_OPENROUTER_API_KEY", "sk-or-secret")
    assert _KEY_TO_FIELD["openrouter_api_key"] == "api_key"
    assert _get("openrouter_api_key") == 0


def test_config_get_masks_the_secret_it_finds(tmp_path, monkeypatch, capsys):
    from qq.configcmd import _get

    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "qq"))
    monkeypatch.setenv("QQ_PROVIDER", "openrouter")
    monkeypatch.setenv("QQ_OPENROUTER_API_KEY", "sk-or-secret")
    _get("openrouter_api_key")
    out = capsys.readouterr().out
    assert "sk-or-secret" not in out
    assert "redacted" in out


# --- project endpoints and the API surface ----------------------------------

PROJECT = "https://qq-dev-abc.services.ai.azure.com/api/projects/qq-dev"


def test_project_endpoint_is_recognised_and_selects_responses():
    s = Settings(endpoint=PROJECT)
    assert s.is_project_endpoint is True
    assert s.effective_api == "responses"
    assert s.base_url == PROJECT + "/openai/v1"


def test_account_endpoint_stays_on_chat_completions():
    s = Settings(endpoint=RESOURCE)
    assert s.is_project_endpoint is False
    assert s.effective_api == "chat"


def test_an_explicit_api_beats_the_endpoint_heuristic():
    assert Settings(endpoint=PROJECT, api="chat").effective_api == "chat"
    assert Settings(endpoint=RESOURCE, api="responses").effective_api == "responses"


def test_a_pasted_project_base_url_normalizes_cleanly():
    assert normalize_endpoint(PROJECT + "/openai/v1/") == PROJECT + "/openai/v1"


def test_openrouter_defaults_to_responses():
    assert Settings(provider="openrouter").effective_api == "responses"
    assert Settings(provider="openrouter", api="chat").effective_api == "chat"


def test_the_project_heuristic_is_azure_only():
    assert Settings(provider="openrouter", endpoint=PROJECT).is_project_endpoint is False


# --- search -----------------------------------------------------------------


def test_search_is_off_by_default():
    assert resolve(env={}, file_values={}).search is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_search_from_the_environment(value):
    assert resolve(env={"QQ_SEARCH": value}, file_values={}).search is True


def test_search_from_the_config_file_as_bool_or_string():
    assert resolve(env={}, file_values={"search": True}).search is True
    assert resolve(env={}, file_values={"search": "true"}).search is True
    assert resolve(env={}, file_values={"search": "off"}).search is False


def test_an_explicit_no_search_flag_beats_an_enabled_environment():
    s = resolve(env={"QQ_SEARCH": "1"}, file_values={}, search=False)
    assert s.search is False
    assert s.sources["search"] == "flag"


def test_nonsense_search_values_are_rejected():
    with pytest.raises(ConfigError):
        resolve(env={"QQ_SEARCH": "maybe"}, file_values={})


def test_brave_key_sources_and_precedence():
    both = {"QQ_BRAVE_API_KEY": "preferred", "BRAVE_API_KEY": "fallback"}
    assert resolve(env=both, file_values={}).brave_api_key == "preferred"
    assert resolve(env={"BRAVE_API_KEY": "fallback"}, file_values={}).brave_api_key == "fallback"
    stored = resolve(env={}, file_values={"brave_api_key": "from-file"})
    assert stored.brave_api_key == "from-file"
    assert stored.sources["brave_api_key"] == "config file"


def test_brave_key_is_a_secret_everywhere_it_is_shown(tmp_path, monkeypatch, capsys):
    from qq.config import SECRET_KEYS
    from qq.configcmd import _get, _show

    assert "brave_api_key" in SECRET_KEYS
    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "qq"))
    monkeypatch.setenv("QQ_BRAVE_API_KEY", "brave-secret-value")
    _get("brave_api_key")
    _show()
    out = capsys.readouterr().out
    assert "brave-secret-value" not in out
    assert "redacted" in out
    assert "search" in out


# --- generic variables never beat a config file ------------------------------


def test_the_config_file_beats_generic_azure_variables():
    """A shell profile set up for another tool must not redirect a configured qq."""
    env = {
        "AZURE_OPENAI_ENDPOINT": "https://elsewhere.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "other-tools-key",
    }
    s = resolve(env=env, file_values={"endpoint": RESOURCE, "api_key": "mine"})
    assert s.endpoint == RESOURCE
    assert s.api_key == "mine"
    assert s.sources["endpoint"] == "config file"


def test_qq_variables_still_beat_the_config_file():
    s = resolve(
        env={"QQ_ENDPOINT": "https://override.openai.azure.com"},
        file_values={"endpoint": RESOURCE},
    )
    assert s.endpoint == "https://override.openai.azure.com"
    assert s.sources["endpoint"] == "env:QQ_ENDPOINT"


def test_generic_fallbacks_apply_to_every_shared_variable():
    env = {
        "AZURE_TENANT_ID": "t-env",
        "AZURE_SUBSCRIPTION_ID": "s-env",
        "BRAVE_API_KEY": "b-env",
        "OPENROUTER_API_KEY": "o-env",
    }
    stored = {"tenant": "t-file", "subscription": "s-file", "brave_api_key": "b-file"}
    s = resolve(env=env, file_values=stored)
    assert (s.tenant, s.subscription, s.brave_api_key) == ("t-file", "s-file", "b-file")

    bare = resolve(env=env, file_values={})
    assert (bare.tenant, bare.subscription, bare.brave_api_key) == ("t-env", "s-env", "b-env")

    openrouter = resolve(
        env=env, file_values={"provider": "openrouter", "openrouter_api_key": "o-file"}
    )
    assert openrouter.api_key == "o-file"
    assert resolve(env=env, file_values={"provider": "openrouter"}).api_key == "o-env"


def test_a_generic_azure_key_is_only_used_with_the_generic_endpoint():
    """Another tool's AZURE_OPENAI_API_KEY must not be sent to qq's configured
    endpoint, nor flip auth=auto away from Entra."""
    s = resolve(
        env={"AZURE_OPENAI_API_KEY": "other-tools-key"},
        file_values={"endpoint": RESOURCE},
    )
    assert s.api_key is None
    assert s.sources["api_key"] == "default"
    assert s.effective_auth == "entra"

    paired = resolve(
        env={"AZURE_OPENAI_ENDPOINT": RESOURCE, "AZURE_OPENAI_API_KEY": "paired-key"},
        file_values={},
    )
    assert paired.api_key == "paired-key"
    assert paired.effective_auth == "key"
