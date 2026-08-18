"""Master-key custody backends: file (default) and OS keyring (opt-in).

The real ``keyring`` package is never required: the keyring backend is
exercised with an in-memory fake, injected either directly into the backend
or via ``sys.modules`` to cover the lazy import path.
"""

from __future__ import annotations

import sys
import types

import pytest
from cryptography.fernet import Fernet

from sovereign.capital.wallet import (
    FileMasterKeyStore,
    KeyringMasterKeyStore,
    MasterKeyStore,
    Wallet,
    master_key_store_from_config,
)
from sovereign.config import EngineConfig


class FakeKeyring:
    """In-memory stand-in for the ``keyring`` module surface the backend uses."""

    def __init__(self) -> None:
        self.storage: dict[tuple[str, str], str] = {}
        self.set_calls = 0

    def get_password(self, service: str, username: str) -> str | None:
        return self.storage.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.set_calls += 1
        self.storage[(service, username)] = password


def test_file_backend_round_trips_and_reuses_key(tmp_path):
    path = tmp_path / "master.key"
    store = FileMasterKeyStore(path)
    key = store.get_or_create_key()
    Fernet(key)  # valid base64 Fernet key
    assert path.read_bytes() == key
    assert (path.stat().st_mode & 0o777) == 0o600
    assert store.get_or_create_key() == key
    assert FileMasterKeyStore(path).get_or_create_key() == key


def test_wallet_defaults_to_file_backend(tmp_path):
    wallet = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    assert isinstance(wallet.key_store, FileMasterKeyStore)
    first = wallet.load_or_create()
    assert (tmp_path / "master.key").exists()
    again = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key").load_or_create()
    assert again.eth_address == first.eth_address


def test_keyring_backend_get_or_creates_persists_and_reuses():
    fake = FakeKeyring()
    store = KeyringMasterKeyStore(service="svc", username="user", keyring_module=fake)
    key = store.get_or_create_key()
    Fernet(key)
    assert fake.set_calls == 1
    assert fake.storage[("svc", "user")] == key.decode("ascii")
    assert store.get_or_create_key() == key
    assert fake.set_calls == 1  # reused, not regenerated
    other = KeyringMasterKeyStore(service="svc", username="user", keyring_module=fake)
    assert other.get_or_create_key() == key


def test_keyring_backend_lazy_imports_module(monkeypatch):
    fake = FakeKeyring()
    module = types.ModuleType("keyring")
    module.get_password = fake.get_password  # type: ignore[attr-defined]
    module.set_password = fake.set_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)
    store = KeyringMasterKeyStore()  # defaults: service=sovereign, username=master_key
    key = store.get_or_create_key()
    assert fake.storage[("sovereign", "master_key")] == key.decode("ascii")
    assert KeyringMasterKeyStore().get_or_create_key() == key


def test_missing_keyring_package_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)  # force ImportError
    store = KeyringMasterKeyStore()
    with pytest.raises(RuntimeError) as excinfo:
        store.get_or_create_key()
    assert "sovereign[keyring]" in str(excinfo.value)


def test_wallet_on_keyring_backend_never_writes_master_key_file(tmp_path):
    fake = FakeKeyring()
    wallet = Wallet(
        tmp_path / "secrets.enc",
        tmp_path / "master.key",
        key_store=KeyringMasterKeyStore(keyring_module=fake),
    )
    bundle = wallet.load_or_create()
    wallet.put_credential("UPWORK_TOKEN", "hunter2")
    assert wallet.get_credential("UPWORK_TOKEN") == "hunter2"
    assert wallet.credential_flags() == {"UPWORK_TOKEN": True}
    token = wallet.encrypt_blob(b"payload")
    assert wallet.decrypt_blob(token) == b"payload"

    # Same keyring entry == same master key: a fresh Wallet can decrypt.
    reopened = Wallet(
        tmp_path / "secrets.enc",
        tmp_path / "master.key",
        key_store=KeyringMasterKeyStore(keyring_module=fake),
    )
    assert reopened.load_or_create().eth_address == bundle.eth_address
    assert reopened.get_credential("UPWORK_TOKEN") == "hunter2"

    assert (tmp_path / "secrets.enc").exists()
    leftovers = {p.name for p in tmp_path.iterdir()}
    assert "master.key" not in leftovers
    assert "master.key.lock" not in leftovers


def test_master_key_store_from_config_selects_backend(tmp_path):
    cfg = EngineConfig(data_dir=tmp_path)
    assert cfg.wallet.master_key_backend == "file"
    assert cfg.wallet.keyring_service == "sovereign"
    assert cfg.wallet.keyring_username == "master_key"
    default_store = master_key_store_from_config(cfg, cfg.paths())
    assert isinstance(default_store, MasterKeyStore)
    assert isinstance(default_store, FileMasterKeyStore)
    assert default_store.master_key_path == cfg.paths().master_key

    cfg_keyring = EngineConfig(
        data_dir=tmp_path,
        wallet={
            "master_key_backend": "keyring",
            "keyring_service": "svc",
            "keyring_username": "user",
        },
    )
    keyring_store = master_key_store_from_config(cfg_keyring, cfg_keyring.paths())
    assert isinstance(keyring_store, KeyringMasterKeyStore)
    assert keyring_store.service == "svc"
    assert keyring_store.username == "user"


def test_rotate_master_key_reencrypts_and_reuses_credentials(tmp_path):
    wallet = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    first = wallet.load_or_create()
    wallet.put_credential("SMTP_PASS", "old-secret")
    old_key = (tmp_path / "master.key").read_bytes()
    old_blob = (tmp_path / "secrets.enc").read_bytes()

    report = wallet.rotate_master_key()
    assert report["ok"] is True
    assert report["backend"] == "file"
    assert (tmp_path / "master.key").read_bytes() != old_key
    assert (tmp_path / "secrets.enc").read_bytes() != old_blob
    assert not (tmp_path / "secrets.enc.rotating").exists()

    reopened = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    assert reopened.load_or_create().eth_address == first.eth_address
    assert reopened.get_credential("SMTP_PASS") == "old-secret"


def test_rotate_recovers_staged_blob_after_key_replace(tmp_path):
    wallet = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    first = wallet.load_or_create()
    wallet.put_credential("TOKEN", "abc")
    # Simulate crash after replace_key + staged blob, before promoting:
    # leave secrets.enc on the old key, rotating on the new key, store on new.
    payload = {
        "wallet": {
            "mnemonic": first.mnemonic,
            "eth_address": first.eth_address,
            "eth_key": first.eth_key,
            "sol_address": first.sol_address,
            "sol_secret": first.sol_secret,
        },
        "credentials": {"TOKEN": "abc"},
    }
    import json as _json

    new_key = Fernet.generate_key()
    staged = Fernet(new_key).encrypt(_json.dumps(payload).encode())
    rotating = tmp_path / "secrets.enc.rotating"
    rotating.write_bytes(staged)
    wallet.key_store.replace_key(new_key)

    recovered = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    assert recovered.load_or_create().eth_address == first.eth_address
    assert recovered.get_credential("TOKEN") == "abc"
    assert not rotating.exists()


def test_migrate_to_keyring_leaves_or_deletes_file(tmp_path):
    fake = FakeKeyring()
    wallet = Wallet(tmp_path / "secrets.enc", tmp_path / "master.key")
    first = wallet.load_or_create()
    wallet.put_credential("X", "1")
    assert (tmp_path / "master.key").exists()

    report = wallet.migrate_key_store(
        KeyringMasterKeyStore(keyring_module=fake),
        delete_old_file=True,
    )
    assert report["ok"] is True
    assert report["backend"] == "keyring"
    assert report["old_file_removed"] is True
    assert not (tmp_path / "master.key").exists()

    reopened = Wallet(
        tmp_path / "secrets.enc",
        tmp_path / "master.key",
        key_store=KeyringMasterKeyStore(keyring_module=fake),
    )
    assert reopened.load_or_create().eth_address == first.eth_address
    assert reopened.get_credential("X") == "1"
