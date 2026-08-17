from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import base58
from cryptography.fernet import Fernet
from eth_account import Account
from mnemonic import Mnemonic
from nacl.signing import SigningKey

Account.enable_unaudited_hdwallet_features()


@dataclass
class WalletBundle:
    mnemonic: str
    eth_address: str
    eth_key: str
    sol_address: str
    sol_secret: str


def _fernet(master_key_path: Path) -> Fernet:
    if master_key_path.exists():
        key = master_key_path.read_bytes()
    else:
        key = Fernet.generate_key()
        master_key_path.write_bytes(key)
        os.chmod(master_key_path, 0o600)
    return Fernet(key)


def generate_bundle() -> WalletBundle:
    mnemo = Mnemonic("english")
    phrase = mnemo.generate(strength=128)
    acct = Account.from_mnemonic(phrase)
    sk = SigningKey.generate()
    sol_secret = base58.b58encode(sk.encode() + sk.verify_key.encode()).decode()
    sol_addr = base58.b58encode(bytes(sk.verify_key)).decode()
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

    def load_or_create(self) -> WalletBundle:
        f = _fernet(self.master_key_path)
        if self.secrets_path.exists():
            raw = json.loads(f.decrypt(self.secrets_path.read_bytes()).decode())
            self.bundle = WalletBundle(**raw["wallet"])
            return self.bundle
        self.bundle = generate_bundle()
        payload = {"wallet": asdict(self.bundle)}
        blob = f.encrypt(json.dumps(payload).encode())
        self.secrets_path.write_bytes(blob)
        os.chmod(self.secrets_path, 0o600)
        return self.bundle

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
