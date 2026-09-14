"""The Entra token cache: correctness of expiry, permissions, and opt-out."""

import json
import stat
import time

import pytest

from qq import tokencache


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "qq"))
    monkeypatch.delenv("QQ_NO_TOKEN_CACHE", raising=False)
    yield


def test_round_trip_returns_a_live_token():
    key = tokencache.cache_key("tenant-a", "scope")
    tokencache.store(key, "tok-abc", time.time() + 3600)
    assert tokencache.load(key) == "tok-abc"


def test_a_token_inside_the_refresh_margin_is_treated_as_expired():
    key = tokencache.cache_key("tenant-a", "scope")
    tokencache.store(key, "tok-abc", time.time() + tokencache.REFRESH_MARGIN_SECONDS - 1)
    assert tokencache.load(key) is None


def test_an_expired_token_is_not_returned():
    key = tokencache.cache_key("tenant-a", "scope")
    tokencache.store(key, "tok-abc", time.time() - 10)
    assert tokencache.load(key) is None


def test_different_tenants_do_not_share_an_entry():
    a = tokencache.cache_key("tenant-a", "scope")
    b = tokencache.cache_key("tenant-b", "scope")
    assert a != b
    tokencache.store(a, "token-a", time.time() + 3600)
    assert tokencache.load(b) is None


def test_the_cache_file_is_owner_only():
    key = tokencache.cache_key("t", "s")
    tokencache.store(key, "tok", time.time() + 3600)
    mode = stat.S_IMODE(tokencache.cache_path().stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_the_cache_key_does_not_contain_the_tenant_in_clear_text():
    assert "tenant-a" not in tokencache.cache_key("tenant-a", "scope")


def test_expired_entries_are_pruned_on_write():
    stale = tokencache.cache_key("old", "s")
    fresh = tokencache.cache_key("new", "s")
    tokencache.store(stale, "x", time.time() - 5)
    tokencache.store(fresh, "y", time.time() + 3600)
    with tokencache.cache_path().open() as fh:
        assert list(json.load(fh)) == [fresh]


def test_a_corrupt_cache_file_is_ignored_rather_than_fatal():
    path = tokencache.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not json")
    assert tokencache.load(tokencache.cache_key("t", "s")) is None


def test_the_cache_can_be_disabled(monkeypatch):
    monkeypatch.setenv("QQ_NO_TOKEN_CACHE", "1")
    key = tokencache.cache_key("t", "s")
    tokencache.store(key, "tok", time.time() + 3600)
    assert tokencache.load(key) is None
    assert not tokencache.cache_path().exists()


def test_clear_removes_the_file():
    tokencache.store(tokencache.cache_key("t", "s"), "tok", time.time() + 3600)
    assert tokencache.cache_path().exists()
    tokencache.clear()
    assert not tokencache.cache_path().exists()


def test_provider_uses_the_cache_and_only_calls_azure_once(monkeypatch):
    """The whole point: a second process must not pay for a second az call."""
    from qq import azure as client

    calls = []

    class FakeAccessToken:
        token = "fresh-token"
        expires_on = time.time() + 3600

    class FakeCredential:
        def get_token(self, scope):
            calls.append(scope)
            return FakeAccessToken()

    monkeypatch.setattr(
        client, "build_credential", lambda tenant=None, subscription=None: FakeCredential()
    )

    first = client.entra_token_provider(scope="scope", tenant="t")
    assert first() == "fresh-token"
    assert len(calls) == 1

    # A brand-new provider stands in for a brand-new qq process.
    second = client.entra_token_provider(scope="scope", tenant="t")
    assert second() == "fresh-token"
    assert len(calls) == 1


def test_cli_credential_never_receives_both_subscription_and_tenant(monkeypatch):
    """'az account get-access-token' rejects --subscription with --tenant, and
    the resulting chain fallback was observed hanging for minutes."""
    from qq import azure

    captured = {}

    class FakeCli:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeChain:
        def __init__(self, *creds):
            pass

    class FakeDefault:
        def __init__(self, **kwargs):
            pass

    import azure.identity as ident

    monkeypatch.setattr(ident, "AzureCliCredential", FakeCli)
    monkeypatch.setattr(ident, "ChainedTokenCredential", FakeChain)
    monkeypatch.setattr(ident, "DefaultAzureCredential", FakeDefault)

    azure.build_credential(tenant="t", subscription="s")
    assert captured == {"subscription": "s"}

    captured.clear()
    azure.build_credential(tenant="t", subscription=None)
    assert captured == {"tenant_id": "t"}
