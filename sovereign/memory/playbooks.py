from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sovereign.engine.world import World

DEFAULT_PLAYBOOKS: dict[str, str] = {
    "hunter": (
        "# Hunter\n"
        "- Score jobs against skills. Ignore anything below fit 0.45 unless Director starved.\n"
        "- Prefer fixed-price, remote, paid in USDC/card.\n"
        "- Never spray 50 identical proposals. Cap 8/day.\n"
        "- Tools: jobs.search, jobs.upsert, human.ask\n"
    ),
    "closer": (
        "# Closer\n"
        "- First 3 lines: their stack, the outcome, the constraint.\n"
        "- Fixed price. Kill-scope sentence. 48h default.\n"
        "- Ask for USDC prepay on new logos.\n"
        "- Tools: brain.complete, mail.send, jobs.upsert, playbook.read\n"
    ),
    "crafter": (
        "# Crafter\n"
        "- Jail: data/work/<job_id>. Do not touch the wallet.\n"
        "- Ship a file + runbook. No theatre.\n"
        "- If blocked on a login, ping Courier and pick the next job.\n"
        "- Tools: craft.produce, files.list_work, brain.complete\n"
    ),
    "trader": (
        "# Trader\n"
        "- Code decides signals. You do not improvise entries.\n"
        "- Only certified strategies. Size from Risk. Halt is halt.\n"
        "- Tools: market.certify, ledger.snapshot\n"
    ),
    "director": (
        "# Director\n"
        "- Fund plays by measured $/hour, not vibes.\n"
        "- Protect the $2k minimum before experiments.\n"
        "- Tools: ledger.snapshot, memory.kv_get, heal.diagnose\n"
    ),
    "improver": (
        "# Improver\n"
        "- Patch playbooks from outcomes. A/B for N missions before promote or revert.\n"
        "- Tools: playbook.write_trial, playbook.promote, memory.kv_set\n"
    ),
    "mechanic": (
        "# Mechanic\n"
        "- Diagnose every tick. Repair what is safe. Ask the human only for keys.\n"
        "- Thaw agents after cooldown if reputation recovered.\n"
        "- Tools: heal.diagnose, heal.repair, governance.thaw\n"
    ),
    "ethics": (
        "# Ethics\n"
        "- No secrets in events, no guaranteed-profit claims, no spray.\n"
        "- Tools: governance.freeze, mail.list\n"
    ),
    "treasurer": (
        "# Treasurer\n"
        "- Invoice on delivery. Do not mint cash. Operating cash is not tradable.\n"
        "- Tools: invoice.issue, invoice.collect, ledger.snapshot\n"
    ),
    "courier": (
        "# Courier\n"
        "- Logins only. Never an approval queue for ordinary work.\n"
        "- Tools: human.ask, mail.list\n"
    ),
    "auditor": (
        "# Auditor\n"
        "- Sample deliveries and trades. Slash empty work. Boost real files.\n"
        "- Tools: files.list_work, jobs.list, invoice.list\n"
    ),
    "risk": (
        "# Risk\n"
        "- Daily/weekly halt. Wall operating cash. Freeze at reputation < 20.\n"
        "- Tools: governance.freeze, ledger.snapshot, market.certify\n"
    ),
    "bookkeeper": (
        "# Bookkeeper\n"
        "- Snapshot trailing 30d vs $2k/$5k/$7k every tick. Do not invent cash.\n"
        "- Tools: ledger.snapshot\n"
    ),
    "operator": (
        "# Operator\n"
        "- Buy infra only after Treasurer+Director quorum. Local process is enough to earn.\n"
        "- Tools: human.ask, ledger.snapshot\n"
    ),
    "publisher": (
        "# Publisher\n"
        "- Package a real delivery into a listed offer. No fake testimonials.\n"
        "- Tools: brain.complete, offers.list, files.list_work\n"
    ),
    "scout": (
        "# Scout\n"
        "- Keep a small catalog of priced offers. Underwrite retainers after labor hits $1.5k trailing.\n"
        "- Tools: offers.list, brain.complete, ledger.snapshot\n"
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


def promote_trial(dir_path: Path, agent: str) -> bool:
    trial = dir_path / f"{agent}.trial.md"
    control = dir_path / f"{agent}.md"
    if not trial.exists():
        return False
    control.write_text(trial.read_text())
    trial.unlink()
    return True


def revert_trial(dir_path: Path, agent: str) -> bool:
    trial = dir_path / f"{agent}.trial.md"
    if trial.exists():
        trial.unlink()
        return True
    return False


def read_playbook_ab(world: "World", agent: str, job_id: str | None = None) -> str:
    """Closer (and others with a trial file) get 50/50 A/B. Variant is recorded on the job."""
    paths = world.config.paths().playbooks
    control = read_playbook(paths, agent)
    trial_p = paths / f"{agent}.trial.md"
    if not trial_p.exists():
        return control
    trial = trial_p.read_text()
    key = f"ab_{agent}"
    ab = dict(world.store.get_kv(key) or {"control_n": 0, "trial_n": 0, "control_usd": 0.0, "trial_usd": 0.0})
    use_trial = False
    if job_id:
        use_trial = (sum(ord(c) for c in job_id) % 2) == 0
    else:
        use_trial = (world.tick % 2) == 0
    if use_trial:
        ab["trial_n"] = int(ab.get("trial_n", 0)) + 1
        world.store.set_kv(key, ab)
        return trial
    ab["control_n"] = int(ab.get("control_n", 0)) + 1
    world.store.set_kv(key, ab)
    return control
