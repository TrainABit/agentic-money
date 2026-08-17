from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar

from sovereign.config import Paths
from sovereign.fileio import atomic_write_text, file_lock
from sovereign.memory.store import iso

T = TypeVar("T")


class HumanInbox:
    """The only human interface: credential / login injection. Not an approval queue."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.paths.ensure()
        self.lock_path = self.paths.human.with_name(self.paths.human.name + ".lock")
        with file_lock(self.lock_path):
            if not self.paths.human.exists():
                self._write_unlocked([])

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.paths.human.exists():
            return []
        raw = json.loads(self.paths.human.read_text())
        if not isinstance(raw, list):
            raise TypeError("human inbox must contain a list")
        return raw

    def _read(self) -> list[dict[str, Any]]:
        with file_lock(self.lock_path, shared=True):
            return self._read_unlocked()

    def _write_unlocked(self, items: list[dict[str, Any]]) -> None:
        atomic_write_text(self.paths.human, json.dumps(items, indent=2), mode=0o600)

    def _write(self, items: list[dict[str, Any]]) -> None:
        with file_lock(self.lock_path):
            self._write_unlocked(items)

    def update(self, fn: Callable[[list[dict[str, Any]]], T]) -> T:
        """Apply one read-modify-write operation while holding the inbox lock."""
        with file_lock(self.lock_path):
            items = self._read_unlocked()
            result = fn(items)
            self._write_unlocked(items)
            return result

    def ask(
        self,
        service: str,
        instruction: str,
        fields: list[str],
        why: str,
    ) -> dict[str, Any]:
        def add(items: list[dict[str, Any]]) -> dict[str, Any]:
            for existing in items:
                if existing.get("service") == service and existing.get("status") == "open":
                    return existing
            item = {
                "id": f"hr_{len(items)+1:04d}",
                "ts": iso(),
                "kind": "login",
                "service": service,
                "instruction": instruction,
                "fields": fields,
                "why": why,
                "status": "open",
                "reply": {},
            }
            items.append(item)
            return item

        return self.update(add)

    def reply(self, request_id: str, fields: dict[str, str]) -> dict[str, Any]:
        with file_lock(self.lock_path):
            items = self._read_unlocked()
            for item in items:
                if item["id"] != request_id:
                    continue
                item["reply"] = dict(fields)
                item["status"] = "filled"
                item["filled_ts"] = iso()
                self._write_unlocked(items)
                replies = []
                if self.paths.human_replies.exists():
                    try:
                        loaded = json.loads(self.paths.human_replies.read_text())
                        replies = loaded if isinstance(loaded, list) else []
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        replies = []
                log = dict(item)
                log["reply"] = {k: "[set]" for k in fields}
                replies.append(log)
                atomic_write_text(self.paths.human_replies, json.dumps(replies, indent=2), mode=0o600)
                return item
        raise KeyError(request_id)

    def open(self) -> list[dict[str, Any]]:
        return [i for i in self._read() if i.get("status") == "open"]

    def all(self) -> list[dict[str, Any]]:
        return self._read()
