from __future__ import annotations

from pathlib import Path
from typing import Any

from sovereign.config import EngineConfig


DEFAULT_PLAYBOOKS: dict[str, str] = {
    "hunter": (
        "# Hunter\n"
        "- Score jobs against skills. Ignore anything below fit 0.45 unless Director starved.\n"
        "- Prefer fixed-price, remote, paid in USDC/card.\n"
        "- Never spray 50 identical proposals. Cap 8/day.\n"
    ),
    "closer": (
        "# Closer\n"
        "- First 3 lines: their stack, the outcome, the constraint.\n"
        "- Fixed price. Kill-scope sentence. 48h default.\n"
        "- Ask for USDC prepay on new logos.\n"
    ),
    "crafter": (
        "# Crafter\n"
        "- Jail: data/work/<job_id>. Do not touch the wallet.\n"
        "- Ship a file + runbook. No theatre.\n"
        "- If blocked on a login, ping Courier and pick the next job.\n"
    ),
    "trader": (
        "# Trader\n"
        "- Code decides signals. You do not improvise entries.\n"
        "- Only certified strategies. Size from Risk. Halt is halt.\n"
    ),
    "director": (
        "# Director\n"
        "- Fund plays by measured $/hour, not vibes.\n"
        "- Protect the $2k minimum before experiments.\n"
    ),
    "improver": (
        "# Improver\n"
        "- Patch playbooks from outcomes. A/B for 20 missions before promote.\n"
    ),
}


def seed_playbooks(dir_path: Path) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, body in DEFAULT_PLAYBOOKS.items():
        p = dir_path / f"{name}.md"
        if not p.exists():
            p.write_text(body)


def read_playbook(dir_path: Path, name: str) -> str:
    p = dir_path / f"{name}.md"
    if p.exists():
        return p.read_text()
    return DEFAULT_PLAYBOOKS.get(name, "")
