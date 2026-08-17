from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sovereign.config import Paths
from sovereign.memory.store import iso


class HumanInbox:
    """The only human interface: credential / login injection. Not an approval queue."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.paths.ensure()
        if not self.paths.human.exists():
            self._write([])

    def _read(self) -> list[dict[str, Any]]:
        return json.loads(self.paths.human.read_text())

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.paths.human.write_text(json.dumps(items, indent=2))

    def ask(
        self,
        service: str,
        instruction: str,
        fields: list[str],
        why: str,
    ) -> dict[str, Any]:
        items = self._read()
        for it in items:
            if it.get("service") == service and it.get("status") == "open":
                return it
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
        self._write(items)
        return item

    def reply(self, request_id: str, fields: dict[str, str]) -> dict[str, Any]:
        items = self._read()
        for it in items:
            if it["id"] == request_id:
                it["reply"] = fields
                it["status"] = "filled"
                it["filled_ts"] = iso()
                self._write(items)
                replies = []
                if self.paths.human_replies.exists():
                    replies = json.loads(self.paths.human_replies.read_text())
                replies.append(it)
                self.paths.human_replies.write_text(json.dumps(replies, indent=2))
                return it
        raise KeyError(request_id)

    def open(self) -> list[dict[str, Any]]:
        return [i for i in self._read() if i.get("status") == "open"]

    def all(self) -> list[dict[str, Any]]:
        return self._read()
