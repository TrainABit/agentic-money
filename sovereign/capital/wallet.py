from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

from typing import TYPE_CHECKING, Any

import base58
from cryptography.fernet import Fernet
from eth_account import Account
from mnemonic import Mnemonic
from nacl.signing import SigningKey

from sovereign.fileio import atomic_write_bytes, file_lock

if TYPE_CHECKING:
    from sovereign.config import EngineConfig, Paths

Account.enable_unaudited_hdwallet_features()


@dataclass
class WalletBundle:
    mnemonic: str
    eth_address: str
    eth_key: str
    sol_address: str
    sol_secret: str


class MasterKeyStore:
    """Source of the Fernet master key that encrypts ``secrets.enc``."""

    backend: str = "unknown"

    def get_or_create_key(self) -> bytes:
        """Return the base64 Fernet key, generating and persisting it once."""
        raise NotImplementedError

    def replace_key(self, key: bytes) -> None:
        """Overwrite the persisted master key with ``key``."""
        raise NotImplementedError


class FileMasterKeyStore(MasterKeyStore):
    """Default custody: the key file co-located with ``secrets.enc``.

    Byte-for-byte the historical ``_fernet`` behavior: read
    ``master_key_path`` when present, else generate a key and write it 0600,
    all under the same ``<name>.lock`` file lock.
    """

    backend = "file"

    def __init__(self, master_key_path: Path) -> None:
        self.master_key_path = master_key_path

    def get_or_create_key(self) -> bytes:
        lock_path = self.master_key_path.with_name(self.master_key_path.name + ".lock")
        with file_lock(lock_path):
            if self.master_key_path.exists():
                return self.master_key_path.read_bytes()
            key = Fernet.generate_key()
            atomic_write_bytes(self.master_key_path, key, mode=0o600)
            return key

    def replace_key(self, key: bytes) -> None:
        lock_path = self.master_key_path.with_name(self.master_key_path.name + ".lock")
        with file_lock(lock_path):
            atomic_write_bytes(self.master_key_path, key, mode=0o600)


class KeyringMasterKeyStore(MasterKeyStore):
    """Opt-in custody in the OS keyring, so the master key never sits on disk
    next to ``secrets.enc``.

    The ``keyring`` import is lazy: the optional dependency is only needed
    when this backend is actually used. ``keyring_module`` allows dependency
    injection (tests pass an in-memory fake).
    """

    backend = "keyring"

    def __init__(
        self,
        service: str = "sovereign",
        username: str = "master_key",
        keyring_module: Any | None = None,
    ) -> None:
        self.service = service
        self.username = username
        self._keyring = keyring_module

    def _module(self) -> Any:
        if self._keyring is None:
            try:
                import keyring
            except ImportError as exc:
                raise RuntimeError(
                    "master_key_backend='keyring' requires the optional "
                    "'keyring' package; install it with: "
                    "pip install 'sovereign[keyring]'"
                ) from exc
            self._keyring = keyring
        return self._keyring

    def get_or_create_key(self) -> bytes:
        kr = self._module()
        stored = kr.get_password(self.service, self.username)
        if stored:
            return stored.encode("ascii")
        key = Fernet.generate_key()
        kr.set_password(self.service, self.username, key.decode("ascii"))
        return key

    def replace_key(self, key: bytes) -> None:
        self._module().set_password(self.service, self.username, key.decode("ascii"))


def master_key_store_from_config(config: "EngineConfig", paths: "Paths") -> MasterKeyStore:
    """Build the master-key backend selected by ``config.wallet``.

    Bootstrap should pass ``Wallet(..., key_store=master_key_store_from_config(
    config, paths))`` when ``config.wallet.master_key_backend == "keyring"``;
    the default file backend needs no wiring.
    """
    if config.wallet.master_key_backend == "keyring":
        return KeyringMasterKeyStore(
            service=config.wallet.keyring_service,
            username=config.wallet.keyring_username,
        )
    return FileMasterKeyStore(paths.master_key)


def _fernet(master_key_path: Path) -> Fernet:
    """Historical helper: Fernet from the file-backed master key."""
    return Fernet(FileMasterKeyStore(master_key_path).get_or_create_key())


def derive_solana_keypair(mnemonic: str) -> tuple[str, str]:
    """Derive Solana account 0 using SLIP-0010 path m/44'/501'/0'/0'."""
    seed = Mnemonic.to_seed(mnemonic, passphrase="")
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key, chain_code = digest[:32], digest[32:]
    for index in (44, 501, 0, 0):
        hardened = index | 0x80000000
        digest = hmac.new(
            chain_code,
            b"\x00" + key + struct.pack(">I", hardened),
            hashlib.sha512,
        ).digest()
        key, chain_code = digest[:32], digest[32:]
    signing_key = SigningKey(key)
    public_key = bytes(signing_key.verify_key)
    address = base58.b58encode(public_key).decode()
    secret = base58.b58encode(key + public_key).decode()
    return address, secret


def generate_bundle(mnemonic: str | None = None) -> WalletBundle:
    mnemo = Mnemonic("english")
    phrase = mnemo.generate(strength=128) if mnemonic is None else mnemonic
    if not mnemo.check(phrase):
        raise ValueError("invalid BIP39 mnemonic")
    acct = Account.from_mnemonic(phrase)
    sol_addr, sol_secret = derive_solana_keypair(phrase)
    return WalletBundle(
        mnemonic=phrase,
        eth_address=acct.address,
        eth_key=acct.key.hex(),
        sol_address=sol_addr,
        sol_secret=sol_secret,
    )


class Wallet:
    def __init__(
        self,
        secrets_path: Path,
        master_key_path: Path,
        key_store: MasterKeyStore | None = None,
    ) -> None:
        self.secrets_path = secrets_path
        self.master_key_path = master_key_path
        # Default (None) keeps today's behavior: file-backed master key at
        # master_key_path. Passing a store (e.g. KeyringMasterKeyStore) is
        # strictly opt-in and changes nothing about secrets.enc handling.
        self.key_store: MasterKeyStore = key_store or FileMasterKeyStore(master_key_path)
        self.bundle: WalletBundle | None = None
        self.lock_path = self.secrets_path.with_name(self.secrets_path.name + ".lock")

    def _fernet(self) -> Fernet:
        return Fernet(self.key_store.get_or_create_key())

    def _rotating_path(self) -> Path:
        return self.secrets_path.with_name(self.secrets_path.name + ".rotating")

    def _promote_rotating(self, f: Fernet) -> dict | None:
        """Finish a crash-interrupted rotation if the staged blob decrypts."""
        rotating = self._rotating_path()
        if not rotating.exists():
            return None
        try:
            blob = rotating.read_bytes()
            raw = json.loads(f.decrypt(blob).decode())
        except Exception:
            return None
        atomic_write_bytes(self.secrets_path, blob, mode=0o600)
        rotating.unlink(missing_ok=True)
        if self.bundle is None and raw.get("wallet"):
            self.bundle = WalletBundle(**raw["wallet"])
        return raw

    def load_or_create(self) -> WalletBundle:
        with file_lock(self.lock_path):
            f = self._fernet()
            promoted = self._promote_rotating(f)
            if promoted is not None:
                assert self.bundle
                return self.bundle
            if self.secrets_path.exists():
                raw = json.loads(f.decrypt(self.secrets_path.read_bytes()).decode())
                self.bundle = WalletBundle(**raw["wallet"])
                return self.bundle
            self.bundle = generate_bundle()
            self._write_unlocked({"wallet": asdict(self.bundle), "credentials": {}}, f=f)
            return self.bundle

    def _read_unlocked(self) -> dict:
        f = self._fernet()
        promoted = self._promote_rotating(f)
        if promoted is not None:
            return promoted
        if not self.secrets_path.exists():
            self.bundle = generate_bundle()
            payload = {"wallet": asdict(self.bundle), "credentials": {}}
            self._write_unlocked(payload, f=f)
            return payload
        raw = json.loads(f.decrypt(self.secrets_path.read_bytes()).decode())
        if self.bundle is None and raw.get("wallet"):
            self.bundle = WalletBundle(**raw["wallet"])
        return raw

    def rotate_master_key(self) -> dict[str, Any]:
        """Re-encrypt ``secrets.enc`` with a newly generated master key.

        The new ciphertext is staged at ``secrets.enc.rotating`` before the
        store is updated, so a crash mid-rotation is recovered on the next
        load (see ``_promote_rotating``). Never returns key material.
        """
        with file_lock(self.lock_path):
            payload = self._read_unlocked()
            new_key = Fernet.generate_key()
            blob = Fernet(new_key).encrypt(json.dumps(payload).encode())
            rotating = self._rotating_path()
            atomic_write_bytes(rotating, blob, mode=0o600)
            self.key_store.replace_key(new_key)
            checked = json.loads(Fernet(new_key).decrypt(rotating.read_bytes()).decode())
            if not checked.get("wallet"):
                raise RuntimeError("rotation produced an unreadable secrets blob")
            atomic_write_bytes(self.secrets_path, rotating.read_bytes(), mode=0o600)
            rotating.unlink(missing_ok=True)
            if checked.get("wallet"):
                self.bundle = WalletBundle(**checked["wallet"])
        return {"ok": True, "backend": self.key_store.backend}

    def migrate_key_store(
        self,
        new_store: MasterKeyStore,
        *,
        delete_old_file: bool = False,
    ) -> dict[str, Any]:
        """Re-encrypt under ``new_store`` and switch the live backend.

        Used to move a file-backed master key into the OS keyring (or the
        other way). The old ``master.key`` is left in place unless
        ``delete_old_file`` is set — deleting it is irreversible.
        """
        with file_lock(self.lock_path):
            payload = self._read_unlocked()
            new_key = Fernet.generate_key()
            blob = Fernet(new_key).encrypt(json.dumps(payload).encode())
            rotating = self._rotating_path()
            atomic_write_bytes(rotating, blob, mode=0o600)
            new_store.replace_key(new_key)
            checked = json.loads(Fernet(new_key).decrypt(rotating.read_bytes()).decode())
            if not checked.get("wallet"):
                raise RuntimeError("key-store migration produced an unreadable secrets blob")
            atomic_write_bytes(self.secrets_path, rotating.read_bytes(), mode=0o600)
            rotating.unlink(missing_ok=True)
            self.key_store = new_store
            if checked.get("wallet"):
                self.bundle = WalletBundle(**checked["wallet"])
            removed = False
            if delete_old_file and self.master_key_path.exists():
                self.master_key_path.unlink()
                removed = True
        return {
            "ok": True,
            "backend": new_store.backend,
            "old_file_removed": removed,
        }

    def _read(self) -> dict:
        with file_lock(self.lock_path):
            return self._read_unlocked()

    def _write_unlocked(self, payload: dict, *, f: Fernet | None = None) -> None:
        f = f or self._fernet()
        blob = f.encrypt(json.dumps(payload).encode())
        atomic_write_bytes(self.secrets_path, blob, mode=0o600)

    def _write(self, payload: dict) -> None:
        with file_lock(self.lock_path):
            self._write_unlocked(payload)

    def put_credential(self, key: str, value: str) -> None:
        with file_lock(self.lock_path):
            raw = self._read_unlocked()
            creds = dict(raw.get("credentials") or {})
            creds[key] = value
            raw["credentials"] = creds
            if "wallet" not in raw and self.bundle:
                raw["wallet"] = asdict(self.bundle)
            self._write_unlocked(raw)

    def get_credential(self, key: str) -> str | None:
        raw = self._read()
        creds = raw.get("credentials") or {}
        val = creds.get(key)
        return str(val) if val else None

    def credential_flags(self) -> dict[str, bool]:
        raw = self._read()
        creds = raw.get("credentials") or {}
        return {k: bool(v) for k, v in creds.items()}

    def encrypt_blob(self, data: bytes) -> bytes:
        with file_lock(self.lock_path):
            return self._fernet().encrypt(data)

    def decrypt_blob(self, token: bytes) -> bytes:
        with file_lock(self.lock_path):
            return self._fernet().decrypt(token)

    def public(self) -> dict[str, str]:
        if not self.bundle:
            self.load_or_create()
        assert self.bundle
        return {
            "eth_address": self.bundle.eth_address,
            "sol_address": self.bundle.sol_address,
        }

    def reveal_mnemonic(self) -> str:
        if os.environ.get("SOVEREIGN_CONFIRM_REVEAL") != "1":
            raise PermissionError("Set SOVEREIGN_CONFIRM_REVEAL=1 to reveal the mnemonic")
        if not self.bundle:
            self.load_or_create()
        assert self.bundle
        return self.bundle.mnemonic
