"""Encrypted at-rest storage for per-site browser sessions.

Playwright storage-state dicts (cookies + localStorage origins) are sealed
with the wallet's master key before they touch disk: one Fernet token per
domain. Files are named by the sha256 of the normalized host so hostile
input cannot influence paths, and an encrypted index maps hash -> host for
listing. Plaintext cookies never touch disk and are never logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken

from sovereign.fileio import atomic_write_bytes, file_lock
from sovereign.memory.store import iso

if TYPE_CHECKING:
    from sovereign.capital.wallet import Wallet

_HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
_INDEX_FILE = "index.enc"
_URL_MARKERS = ("//", "/", ":", "?", "#", "@")


class WebVault:
    """One encrypted session file per domain, plus an encrypted index."""

    def __init__(self, wallet: Wallet, sessions_dir: Path) -> None:
        self.wallet = wallet
        self.sessions_dir = Path(sessions_dir)
        self.lock_path = self.sessions_dir.with_name(self.sessions_dir.name + ".lock")

    @staticmethod
    def _domain_key(domain: str) -> str:
        """Normalized hostname for *domain*, which may be a bare host or a URL."""
        candidate = str(domain or "").strip().lower()
        if any(marker in candidate for marker in _URL_MARKERS):
            split = urlsplit(candidate if "//" in candidate else "//" + candidate)
            candidate = split.hostname or ""
        host = candidate.rstrip(".")
        if (
            not host
            or not _HOST_RE.match(host)
            or ".." in host
            or host.startswith((".", "-"))
            or host.endswith("-")
        ):
            raise ValueError(
                f"invalid domain {domain!r}: expected a hostname like example.com"
            )
        return host

    def _digest(self, host: str) -> str:
        return hashlib.sha256(host.encode("ascii")).hexdigest()

    def _session_path(self, host: str) -> Path:
        return self.sessions_dir / (self._digest(host) + ".enc")

    def _ensure_dir(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.sessions_dir, 0o700)

    def _read_index_unlocked(self) -> dict[str, dict[str, Any]]:
        index_path = self.sessions_dir / _INDEX_FILE
        if not index_path.exists():
            return {}
        try:
            raw = json.loads(self.wallet.decrypt_blob(index_path.read_bytes()).decode("utf-8"))
        except (InvalidToken, ValueError, OSError, UnicodeDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): dict(entry) for key, entry in raw.items() if isinstance(entry, dict)}

    def _write_index_unlocked(self, index: dict[str, dict[str, Any]]) -> None:
        self._ensure_dir()
        blob = self.wallet.encrypt_blob(json.dumps(index).encode("utf-8"))
        atomic_write_bytes(self.sessions_dir / _INDEX_FILE, blob, mode=0o600)

    def save_session(self, domain: str, storage_state: dict) -> None:
        host = self._domain_key(domain)
        token = self.wallet.encrypt_blob(json.dumps(storage_state).encode("utf-8"))
        with file_lock(self.lock_path):
            self._ensure_dir()
            atomic_write_bytes(self._session_path(host), token, mode=0o600)
            index = self._read_index_unlocked()
            index[self._digest(host)] = {"host": host, "saved_ts": iso()}
            self._write_index_unlocked(index)

    def load_session(self, domain: str) -> dict | None:
        path = self._session_path(self._domain_key(domain))
        try:
            token = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            state = json.loads(self.wallet.decrypt_blob(token).decode("utf-8"))
        except (InvalidToken, ValueError, OSError, UnicodeDecodeError):
            # Undecryptable or mangled: report absence but keep the file
            # in place so an operator can inspect what happened.
            return None
        return state if isinstance(state, dict) else None

    def has_session(self, domain: str) -> bool:
        return self._session_path(self._domain_key(domain)).exists()

    def list_domains(self) -> list[str]:
        """Normalized hostnames with a vaulted session — never secrets."""
        with file_lock(self.lock_path, shared=True):
            index = self._read_index_unlocked()
        hosts = {str(entry.get("host")) for entry in index.values() if entry.get("host")}
        return sorted(hosts)

    def delete_session(self, domain: str) -> bool:
        host = self._domain_key(domain)
        with file_lock(self.lock_path):
            path = self._session_path(host)
            existed = path.exists()
            if existed:
                path.unlink()
            index = self._read_index_unlocked()
            dropped = index.pop(self._digest(host), None) is not None
            if existed or dropped:
                self._write_index_unlocked(index)
        return existed
