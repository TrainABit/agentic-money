from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

import base58
from cryptography.fernet import Fernet
from eth_account import Account
from mnemonic import Mnemonic
from nacl.signing import SigningKey

from sovereign.fileio import atomic_write_bytes, file_lock

Account.enable_unaudited_hdwallet_features()


@dataclass
class WalletBundle:
    mnemonic: str
    eth_address: str
    eth_key: str
    sol_address: str
    sol_secret: str


def _fernet(master_key_path: Path) -> Fernet:
    lock_path = master_key_path.with_name(master_key_path.name + ".lock")
    with file_lock(lock_path):
        if master_key_path.exists():
            key = master_key_path.read_bytes()
        else:
            key = Fernet.generate_key()
            atomic_write_bytes(master_key_path, key, mode=0o600)
    return Fernet(key)


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
    def __init__(self, secrets_path: Path, master_key_path: Path) -> None:
        self.secrets_path = secrets_path
        self.master_key_path = master_key_path
        self.bundle: WalletBundle | None = None
        self.lock_path = self.secrets_path.with_name(self.secrets_path.name + ".lock")

    def load_or_create(self) -> WalletBundle:
        with file_lock(self.lock_path):
            f = _fernet(self.master_key_path)
            if self.secrets_path.exists():
                raw = json.loads(f.decrypt(self.secrets_path.read_bytes()).decode())
                self.bundle = WalletBundle(**raw["wallet"])
                return self.bundle
            self.bundle = generate_bundle()
            self._write_unlocked({"wallet": asdict(self.bundle), "credentials": {}}, f=f)
            return self.bundle

    def _read_unlocked(self) -> dict:
        f = _fernet(self.master_key_path)
        if not self.secrets_path.exists():
            self.bundle = generate_bundle()
            payload = {"wallet": asdict(self.bundle), "credentials": {}}
            self._write_unlocked(payload, f=f)
            return payload
        raw = json.loads(f.decrypt(self.secrets_path.read_bytes()).decode())
        if self.bundle is None and raw.get("wallet"):
            self.bundle = WalletBundle(**raw["wallet"])
        return raw

    def _read(self) -> dict:
        with file_lock(self.lock_path):
            return self._read_unlocked()

    def _write_unlocked(self, payload: dict, *, f: Fernet | None = None) -> None:
        f = f or _fernet(self.master_key_path)
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
            return _fernet(self.master_key_path).encrypt(data)

    def decrypt_blob(self, token: bytes) -> bytes:
        with file_lock(self.lock_path):
            return _fernet(self.master_key_path).decrypt(token)

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
