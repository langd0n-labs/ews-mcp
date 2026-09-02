import pytest

msal = pytest.importorskip("msal")
exchangelib = pytest.importorskip("exchangelib")

from src.auth import O365DeviceCodeCredentials


class FakeCache:
    has_state_changed = False

    def deserialize(self, value):
        self.loaded = value

    def serialize(self):
        return '{"Cache": {}}'


class FakeApp:
    def __init__(self, results, accounts):
        self.results = iter(results)
        self.accounts = accounts
        self.silent_calls = 0
        self.device_calls = 0

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        self.silent_calls += 1
        return next(self.results)

    def initiate_device_flow(self, scopes):
        return {"user_code": "test-code", "message": "https://example.test code"}

    def acquire_token_by_device_flow(self, flow):
        self.device_calls += 1
        return {"access_token": "value"}


def make_credentials(monkeypatch, tmp_path, results, accounts):
    cache = FakeCache()
    app = FakeApp(results, accounts)
    monkeypatch.setattr(msal, "SerializableTokenCache", lambda: cache)
    monkeypatch.setattr(msal, "PublicClientApplication", lambda **kwargs: app)
    credentials = O365DeviceCodeCredentials("client", "authority", str(tmp_path / "cache.json"))
    return credentials, app


def test_uses_device_flow_when_no_cached_account(monkeypatch, tmp_path, capsys):
    credentials, app = make_credentials(monkeypatch, tmp_path, [], [])

    assert credentials.access_token["access_token"] == "value"
    assert app.device_calls == 1
    assert "https://example.test" in capsys.readouterr().out
    assert (tmp_path / "cache.json").stat().st_mode & 0o777 == 0o600


def test_refresh_uses_cached_account_without_device_prompt(monkeypatch, tmp_path):
    credentials, app = make_credentials(
        monkeypatch,
        tmp_path,
        [{"access_token": "value"}],
        [{"username": "user@example.test"}],
    )

    assert app.silent_calls == 1
    assert app.device_calls == 0
    app.results = iter([{"access_token": "value"}])
    assert credentials.refresh(session=object())["access_token"] == "value"
    assert app.silent_calls == 2
