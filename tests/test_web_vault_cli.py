from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from sovereign.capital.wallet import Wallet
from sovereign.cli import main
from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap
from sovereign.web.login import (
    capture_headful_login,
    import_session_file,
    normalize_storage_state,
    request_web_login,
)
from sovereign.web.vault import WebVault

SECRET = "super-secret-session-cookie-value-4242"


def _state(secret: str = SECRET) -> dict:
    return {
        "cookies": [
            {
                "name": "sid",
                "value": secret,
                "domain": ".example.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
            }
        ],
        "origins": [
            {
                "origin": "https://example.com",
                "localStorage": [{"name": "token", "value": secret}],
            }
        ],
    }


def _vault(tmp_path: Path) -> WebVault:
    wallet = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    return WebVault(wallet, tmp_path / "web_sessions")


def test_wallet_blob_roundtrip_and_ciphertext(tmp_path):
    wallet = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    data = b"cookie-jar \x00 arbitrary bytes " + SECRET.encode()
    token = wallet.encrypt_blob(data)
    assert token != data
    assert data not in token
    assert SECRET.encode() not in token
    assert wallet.decrypt_blob(token) == data
    # Same master key as the credential methods: a fresh Wallet on the same
    # paths decrypts the token.
    other = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    assert other.decrypt_blob(token) == data


def test_vault_save_load_has_list_delete_roundtrip(tmp_path):
    vault = _vault(tmp_path)
    assert vault.load_session("example.com") is None
    assert not vault.has_session("example.com")
    assert vault.list_domains() == []

    vault.save_session("example.com", _state())
    vault.save_session("https://app.example.com/login", _state("other-secret"))

    assert vault.has_session("example.com")
    assert vault.has_session("app.example.com")
    assert vault.load_session("example.com") == _state()
    assert vault.load_session("https://example.com/") == _state()
    assert vault.list_domains() == ["app.example.com", "example.com"]

    assert vault.delete_session("app.example.com") is True
    assert vault.delete_session("app.example.com") is False
    assert vault.list_domains() == ["example.com"]
    assert not vault.has_session("app.example.com")
    assert vault.load_session("app.example.com") is None


def test_vault_disk_is_ciphertext_with_tight_modes(tmp_path):
    vault = _vault(tmp_path)
    vault.save_session("example.com", _state())
    sessions_dir = tmp_path / "web_sessions"
    files = [p for p in sessions_dir.rglob("*") if p.is_file()]
    assert files
    for path in files:
        raw = path.read_bytes()
        assert SECRET.encode() not in raw
        assert b"example.com" not in raw
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "example.com" not in path.name
    assert stat.S_IMODE(sessions_dir.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("example.com", "example.com"),
        ("Sub.Example.COM", "sub.example.com"),
        ("example.com.", "example.com"),
        ("https://Example.COM:8443/login?next=/dash#frag", "example.com"),
        ("example.com:443", "example.com"),
        ("user@example.com", "example.com"),
        ("192.168.0.1", "192.168.0.1"),
    ],
)
def test_domain_key_normalizes_urls_and_hosts(given, expected):
    assert WebVault._domain_key(given) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "..",
        "../../etc/passwd",
        "exa mple.com",
        "bad_host!.com",
        "a..b.com",
        ".leading.dot",
        "https://",
        "x" * 300,
    ],
)
def test_domain_key_rejects_bad_and_traversal_hosts(bad, tmp_path):
    with pytest.raises(ValueError):
        WebVault._domain_key(bad)
    vault = _vault(tmp_path)
    with pytest.raises(ValueError):
        vault.save_session(bad, _state())


def test_corrupt_session_file_loads_as_none_and_stays(tmp_path):
    vault = _vault(tmp_path)
    vault.save_session("example.com", _state())
    session_file = vault._session_path("example.com")
    session_file.write_bytes(b"garbage-not-a-fernet-token")
    assert vault.load_session("example.com") is None
    assert session_file.exists()
    # A trashed index degrades listing gracefully too.
    (tmp_path / "web_sessions" / "index.enc").write_bytes(b"junk")
    assert vault.list_domains() == []


def test_normalize_storage_state_accepts_valid_and_rejects_junk():
    clean = normalize_storage_state(
        {"cookies": [{"name": "a", "value": "b"}], "origins": [], "unknown": {"x": 1}}
    )
    assert clean == {"cookies": [{"name": "a", "value": "b"}], "origins": []}
    assert normalize_storage_state({"cookies": []}) == {"cookies": [], "origins": []}
    for junk in (
        None,
        [],
        "cookies",
        {},
        {"cookies": "nope"},
        {"cookies": [1, 2]},
        {"cookies": [], "origins": "nope"},
        {"cookies": [], "origins": ["nope"]},
    ):
        with pytest.raises(ValueError):
            normalize_storage_state(junk)


def test_import_session_file_vaults_and_reports_counts_only(tmp_path):
    vault = _vault(tmp_path)
    exported = tmp_path / "state.json"
    payload = dict(_state(), junk_top_level="dropped")
    exported.write_text(json.dumps(payload))
    report = import_session_file(vault, "https://Example.com/login", exported)
    assert report == {"domain": "example.com", "cookies": 1, "origins": 1}
    assert SECRET not in json.dumps(report)
    assert vault.load_session("example.com") == _state()

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError):
        import_session_file(vault, "example.com", bad)
    with pytest.raises(ValueError):
        import_session_file(vault, "example.com", tmp_path / "missing.json")


def test_request_web_login_files_prefixed_idempotent_ask(tmp_path):
    world = bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))
    first = request_web_login(world, "github.com", "https://github.com/login")
    again = request_web_login(world, "github.com", "https://github.com/login")
    assert first["id"] == again["id"]
    open_asks = [i for i in world.human.open() if i["service"] == "web:github.com"]
    assert len(open_asks) == 1
    ask = open_asks[0]
    assert ask["fields"] == ["done"]
    assert "web-login github.com --import" in ask["instruction"]
    assert "--headful" in ask["instruction"]
    assert "CAPTCHA" in ask["why"]


class FakeDriver:
    def __init__(self, state: dict, *, fail: bool = False) -> None:
        self._state = state
        self._fail = fail
        self.visited: list[str] = []
        self.stopped = False

    def goto(self, url: str) -> None:
        self.visited.append(url)

    def storage_state(self) -> dict:
        if self._fail:
            raise RuntimeError("browser crashed")
        return dict(self._state)

    def stop(self) -> None:
        self.stopped = True


def test_capture_headful_login_drives_injected_fake(monkeypatch, capsys):
    driver = FakeDriver(_state())
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    state = capture_headful_login(
        "https://example.com/login", driver_factory=lambda: driver, timeout_s=5
    )
    assert state == _state()
    assert driver.visited == ["https://example.com/login"]
    assert driver.stopped is True
    assert prompts, "must block on input() for the human"
    assert "example.com/login" in capsys.readouterr().out


def test_capture_headful_login_always_stops_driver(monkeypatch):
    driver = FakeDriver(_state(), fail=True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    with pytest.raises(RuntimeError):
        capture_headful_login("https://example.com", driver_factory=lambda: driver)
    assert driver.stopped is True


def test_cli_web_login_import_then_sessions_never_prints_secrets(tmp_path, capsys):
    data_dir = tmp_path / "data"
    exported = tmp_path / "state.json"
    exported.write_text(json.dumps(_state()))

    code = main(["web-sessions", "--data-dir", str(data_dir)])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == []

    code = main(
        [
            "web-login",
            "Example.com",
            "--data-dir",
            str(data_dir),
            "--import",
            str(exported),
        ]
    )
    out_login = capsys.readouterr().out
    assert code == 0
    assert json.loads(out_login) == {
        "ok": True,
        "domain": "example.com",
        "cookies": 1,
        "origins": 1,
    }

    code = main(["web-sessions", "--data-dir", str(data_dir)])
    out_sessions = capsys.readouterr().out
    assert code == 0
    assert json.loads(out_sessions) == ["example.com"]
    assert SECRET not in out_login + out_sessions

    sessions_dir = data_dir / "web_sessions"
    stored = [p for p in sessions_dir.rglob("*") if p.is_file()]
    assert stored
    for path in stored:
        assert SECRET.encode() not in path.read_bytes()


def test_cli_web_login_without_flags_creates_human_request(tmp_path, capsys):
    data_dir = tmp_path / "data"
    code = main(["web-login", "https://Example.com/login", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert code == 0
    request = json.loads(out)
    assert request["service"] == "web:example.com"
    assert request["status"] == "open"
    assert request["fields"] == ["done"]
    assert SECRET not in out
    world = bootstrap(EngineConfig(mode="sim", data_dir=data_dir))
    assert any(i["service"] == "web:example.com" for i in world.human.open())


def test_cli_web_login_rejects_bad_domain_before_filing_ask(tmp_path, capsys):
    data_dir = tmp_path / "data"
    code = main(["web-login", "../../etc/passwd", "--data-dir", str(data_dir)])
    captured = capsys.readouterr()
    assert code == 1
    assert "invalid domain" in captured.err
    assert "Traceback" not in captured.err
    world = bootstrap(EngineConfig(mode="sim", data_dir=data_dir))
    assert not any(i["service"].startswith("web:") for i in world.human.open())


class HeadfulFakeDriver:
    """Matches the BrowserDriver lifecycle the CLI factory must honor."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.visited: list[str] = []

    def start(self) -> None:
        self.started = True

    def goto(self, url: str, *, timeout_ms: int | None = None) -> None:
        assert self.started, "goto before start()"
        self.visited.append(url)

    def storage_state(self) -> dict:
        return _state("headful-captured-secret")

    def stop(self) -> None:
        self.stopped = True


def test_cli_web_login_headful_captures_via_patched_factory(tmp_path, capsys, monkeypatch):
    import sovereign.web.session as web_session

    driver = HeadfulFakeDriver()
    seen_configs: list[object] = []

    def factory(config):
        seen_configs.append(config)
        return driver

    monkeypatch.setattr(web_session, "default_driver_factory", factory)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    data_dir = tmp_path / "data"
    code = main(["web-login", "example.com", "--headful", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert code == 0
    assert '"domain": "example.com"' in out
    assert "headful-captured-secret" not in out
    assert driver.started and driver.stopped
    assert driver.visited == ["https://example.com/"]
    assert seen_configs and seen_configs[0].headless is False

    code = main(["web-sessions", "--data-dir", str(data_dir)])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == ["example.com"]


def test_cli_web_login_headful_unavailable_prints_hint(tmp_path, capsys, monkeypatch):
    import sovereign.web.session as web_session

    def broken(config):
        raise RuntimeError("web automation requires the [web] extra")

    monkeypatch.setattr(web_session, "default_driver_factory", broken)
    code = main(["web-login", "example.com", "--headful", "--data-dir", str(tmp_path / "data")])
    out = capsys.readouterr().out
    assert code == 1
    envelope = json.loads(out)
    assert envelope["ok"] is False
    assert "web" in envelope["error"]
    assert envelope["hint"] == "use --import with an exported storage_state json"
