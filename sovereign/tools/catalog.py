from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from sovereign.agents.spec import spec_for, system_prompt_for, tool_matrix
from sovereign.capital import invoice as invoices
from sovereign.capital.invariants import verify_invariants
from sovereign.channels import mail as mailbox
from sovereign.labor.craft import produce
from sovereign.labor.pipeline import accept_job
from sovereign.markets.data import certify
from sovereign.memory.playbooks import DEFAULT_PLAYBOOKS
from sovereign.security import job_child, safe_child, validate_job_id
from sovereign.tools.base import Registry, Tool

NOTIFY_CAP_PER_TICK = 5
NOTIFY_GUARD_KEY = "comms_notify_guard"

# (name, description, fn, wants_caller). Allowlists are NOT declared here:
# they are derived from AGENT_SPECS so prompts and enforcement cannot drift.
ToolDef = tuple[str, str, Callable[..., Any], bool]


def _t(name: str, desc: str, fn: Callable[..., Any], *, wants_caller: bool = False) -> ToolDef:
    return (name, desc, fn, wants_caller)


def _brain_complete(w, caller: str, prompt: str, tier: str | None = None, system: str | None = None):
    # `system` is accepted for backward compatibility but always discarded:
    # the caller's fixed spec prompt is authoritative, so neither callers nor
    # untrusted runtime text can rewrite an agent's identity.
    del system
    spec = spec_for(caller)
    return w.router.complete(prompt, tier=tier or spec.tier or "fast", system=system_prompt_for(caller))


def _tool_defs() -> list[ToolDef]:
    return [
        _t("jobs.search", "Search job boards",
           lambda w, live=False: w.board.search(tick=w.tick, live=live, include_sim=w.config.mode == "sim")),
        _t("jobs.list", "List jobs by status",
           lambda w, status=None: w.store.jobs(status)),
        _t("jobs.get", "Get one job",
           lambda w, job_id: w.store.get_job(job_id)),
        _t("jobs.upsert", "Create or update a job",
           lambda w, job: (w.store.upsert_job(job), job)[1]),
        _t("jobs.accept", "Mark a job accepted",
           lambda w, job_id, source="tool": accept_job(w, job_id, source=source)),
        _t("jobs.reject", "Mark a job rejected",
           lambda w, job_id, source="tool": _reject(w, job_id, source)),
        _t("mail.send", "Send email (SMTP or local outbox)",
           lambda w, to, subject, body, job_id=None, kind="outbound": mailbox.send(w, to, subject, body, job_id, kind)),
        _t("mail.list", "List mailbox",
           lambda w, direction=None: w.store.mail(direction=direction)),
        _t("invoice.issue", "Issue a USDC invoice",
           lambda w, job, income_account="income.labor": invoices.issue(w, job, income_account=income_account)),
        _t("invoice.collect", "Mark invoice paid / settle receivable",
           lambda w, ref, source="tool": invoices.collect(w, ref, source=source)),
        _t("invoice.list", "List invoices",
           lambda w, status=None: w.store.invoices(status)),
        _t("ledger.snapshot", "Balances, trailing revenue, equity",
           lambda w: w.ledger.snapshot(now=w.now)),
        _t("wallet.public", "Public ETH/SOL addresses",
           lambda w: w.wallet.public()),
        _t("human.ask", "Request a login/credential from the human",
           lambda w, service, instruction, fields, why: w.human.ask(service, instruction, fields, why)),
        _t("brain.complete", "Language model (Claude subscription or sim brain)",
           _brain_complete, wants_caller=True),
        _t("craft.produce", "Write deliverable files in the job jail",
           lambda w, job: produce(w, job)),
        _t("market.certify", "Walk-forward certify trading strategies",
           lambda w: _certify(w)),
        _t("playbook.read", "Read an agent playbook (A/B aware for closer)",
           lambda w, agent, job_id=None: _read_pb(w, agent, job_id)),
        _t("playbook.write_trial", "Write a trial playbook for A/B",
           lambda w, agent, body: _write_trial(w, agent, body)),
        _t("playbook.promote", "Promote trial playbook to control",
           lambda w, agent: _promote(w, agent)),
        _t("governance.freeze", "Freeze an agent",
           lambda w, target, reason, kind=None: w.freeze(target, reason, kind=kind)),
        _t("governance.thaw", "Thaw a frozen agent",
           lambda w, target, reason: w.thaw(target, reason)),
        _t("memory.kv_get", "Read kv memory",
           lambda w, key, default=None: w.store.get_kv(key, default)),
        _t("memory.kv_set", "Write kv memory",
           lambda w, key, value: (w.store.set_kv(key, value), True)[1]),
        _t("heal.diagnose", "Run engine health checks",
           lambda w: _diagnose(w)),
        _t("heal.repair", "Auto-repair repairable findings",
           lambda w, full=False: _repair(w, full)),
        _t("offers.list", "Listed productized offers",
           lambda w: w.store.offers()),
        _t("files.list_work", "List jailed work files",
           lambda w, job_id: _list_work(w, job_id)),
        _t("knowledge.remember", "Store a lesson in the caller's knowledge memory",
           _knowledge_remember, wants_caller=True),
        _t("knowledge.recall", "Recall lessons from the caller's (plus shared) knowledge memory",
           _knowledge_recall, wants_caller=True),
        _t("knowledge.share", "Publish a lesson into the shared firm knowledge namespace",
           _knowledge_share, wants_caller=True),
        _t("ledger.verify_invariants", "Cross-check ledger balances against invoices and the broker",
           lambda w: verify_invariants(w)),
        _t("ledger.export", "Export every ledger row to a timestamped CSV artifact",
           _ledger_export),
        _t("comms.notify", "Send a rate-capped notify to named agents over the bus",
           _comms_notify, wants_caller=True),
    ]


def validate_matrix(matrix: Mapping[str, frozenset[str]], catalog: Iterable[str]) -> None:
    """Fail loudly when specs and catalog drift apart, in either direction."""
    catalog_names = frozenset(catalog)
    unknown = sorted(set(matrix) - catalog_names)
    if unknown:
        raise RuntimeError(f"agent specs reference tools missing from the catalog: {unknown}")
    orphaned = sorted(catalog_names - set(matrix))
    if orphaned:
        raise RuntimeError(f"catalog tools granted to no agent spec: {orphaned}")
    empty = sorted(name for name, agents in matrix.items() if not agents)
    if empty:
        raise RuntimeError(f"tools with an empty allowlist: {empty}")


def build_registry() -> Registry:
    defs = _tool_defs()
    matrix = tool_matrix()
    validate_matrix(matrix, (name for name, _, _, _ in defs))
    r = Registry()
    for name, desc, fn, wants_caller in defs:
        r.register(Tool(name, desc, matrix[name], fn, wants_caller=wants_caller))
    return r


def _reject(w, job_id: str, source: str = "tool"):
    from sovereign.labor.pipeline import reject_job

    return reject_job(w, job_id, source=source)


def _certify(w) -> list[dict[str, Any]]:
    import numpy as np

    from sovereign.engine.world import load_prices

    w.certified = []
    load_prices(w)
    reports = certify(np.array(w.market_close, dtype=float), w.config.risk)
    w.certified = reports
    w.store.set_kv("certified", reports)
    return reports


def _read_pb(w, agent: str, job_id=None) -> str:
    from sovereign.memory.playbooks import read_playbook_ab

    agent = _playbook_agent(agent)
    clean_job_id = validate_job_id(job_id) if job_id is not None else None
    return read_playbook_ab(w, agent, clean_job_id)


def _write_trial(w, agent: str, body: str) -> str:
    agent = _playbook_agent(agent)
    path = safe_child(w.config.paths().playbooks, f"{agent}.trial.md", label="playbook")
    path.write_text(body)
    return str(path)


def _promote(w, agent: str) -> bool:
    from sovereign.memory.playbooks import promote_trial

    return promote_trial(w.config.paths().playbooks, _playbook_agent(agent))


def _playbook_agent(agent: object) -> str:
    if not isinstance(agent, str) or agent not in DEFAULT_PLAYBOOKS:
        raise ValueError("invalid playbook agent")
    return agent


def _diagnose(w) -> dict[str, Any]:
    from sovereign.heal.checks import diagnose

    return {"findings": [f.as_dict() for f in diagnose(w)]}


def _repair(w, full: bool = False) -> dict[str, Any]:
    from sovereign.heal.repair import setup

    return setup(w, full=full)


def _list_work(w, job_id: str) -> list[str]:
    p = job_child(w.config.paths().work, job_id)
    if not p.exists():
        return []
    return sorted(x.name for x in p.iterdir() if x.is_file() and not x.is_symlink())


def _knowledge_base(w):
    kb = getattr(w, "knowledge", None)
    if kb is None:
        raise RuntimeError("knowledge base unavailable")
    return kb


def _knowledge_remember(w, caller: str, topic: str, content: str, source: str = "self", confidence: float = 0.6):
    # The authenticated caller is the only writable namespace; there is no
    # way to pass another agent's name through this tool.
    return _knowledge_base(w).remember(
        caller, topic, content, now=w.now, source=source, confidence=confidence
    )


def _knowledge_recall(w, caller: str, query: str, limit: int = 5):
    limit = max(1, min(10, int(limit)))
    return _knowledge_base(w).recall(caller, query, now=w.now, limit=limit)


def _knowledge_share(w, caller: str, topic: str, content: str, confidence: float = 0.6):
    # Shared namespace writes are attributed to the caller via `source`.
    return _knowledge_base(w).remember(
        "firm", topic, content, now=w.now, source=caller, confidence=confidence
    )


def _ledger_export(w) -> dict[str, Any]:
    exports = w.config.paths().artifacts / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    stamp = w.now.strftime("%Y%m%dT%H%M%SZ")
    path = exports / f"ledger_{stamp}.csv"
    rows = w.store.ledger_rows()
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "debit", "credit", "amount", "memo", "ref"])
        for row in rows:
            writer.writerow(
                [row["ts"], row["debit"], row["credit"], row["amount"], row["memo"], row["ref"]]
            )
    return {"path": str(path), "rows": len(rows)}


def _comms_notify(w, caller: str, recipients, payload) -> dict[str, Any]:
    if w.comms is None:
        raise RuntimeError("comms bus unavailable")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    if isinstance(recipients, str):
        targets = [recipients]
    elif isinstance(recipients, (list, tuple)):
        targets = list(recipients)
    else:
        raise ValueError("recipients must be a string or a list of strings")
    if not targets or not all(isinstance(r, str) and r for r in targets):
        raise ValueError("recipients must be a string or a list of strings")
    # One shared per-tick budget across ALL callers; only successful sends
    # consume it, and the bus itself validates recipients against the roster.
    guard = w.store.get_kv(NOTIFY_GUARD_KEY) or {}
    count = int(guard.get("count", 0)) if guard.get("tick") == w.tick else 0
    if count >= NOTIFY_CAP_PER_TICK:
        raise ValueError("notify rate cap reached")
    receipt = w.comms.send(caller, targets, "notify", payload, now=w.now)
    w.store.set_kv(NOTIFY_GUARD_KEY, {"tick": w.tick, "count": count + 1})
    return {
        "thread_id": receipt.thread_id,
        "correlation_id": receipt.correlation_id,
        "message_ids": list(receipt.message_ids),
        "recipients": targets,
    }
