from __future__ import annotations

from typing import Any

from sovereign.capital import invoice as invoices
from sovereign.channels import mail as mailbox
from sovereign.labor.craft import produce
from sovereign.labor.pipeline import accept_job
from sovereign.markets.data import certify
from sovereign.tools.base import Registry, Tool


ALL = frozenset({"*"})
LABOR = frozenset({"hunter", "closer", "crafter", "treasurer", "auditor", "director", "bookkeeper", "courier"})
GOV = frozenset({"director", "risk", "auditor", "mechanic", "ethics", "improver", "treasurer"})


def _t(name: str, desc: str, allow: frozenset[str], fn) -> Tool:
    return Tool(name, desc, allow, fn)


def build_registry() -> Registry:
    r = Registry()
    tools = [
        _t("jobs.search", "Search job boards", frozenset({"hunter", "scout", "mechanic"}),
           lambda w, live=False: w.board.search(tick=w.tick, live=live, include_sim=w.config.mode == "sim")),
        _t("jobs.list", "List jobs by status", LABOR | frozenset({"mechanic"}),
           lambda w, status=None: w.store.jobs(status)),
        _t("jobs.get", "Get one job", LABOR,
           lambda w, job_id: w.store.get_job(job_id)),
        _t("jobs.upsert", "Create or update a job", frozenset({"hunter", "closer", "crafter", "treasurer", "publisher", "scout"}),
           lambda w, job: (w.store.upsert_job(job), job)[1]),
        _t("jobs.accept", "Mark a job accepted", frozenset({"closer", "courier", "mechanic"}),
           lambda w, job_id, source="tool": accept_job(w, job_id, source=source)),
        _t("jobs.reject", "Mark a job rejected", frozenset({"closer", "courier", "mechanic"}),
           lambda w, job_id, source="tool": _reject(w, job_id, source)),
        _t("mail.send", "Send email (SMTP or local outbox)", frozenset({"closer", "crafter", "treasurer", "courier", "publisher"}),
           lambda w, to, subject, body, job_id=None, kind="outbound": mailbox.send(w, to, subject, body, job_id, kind)),
        _t("mail.list", "List mailbox", frozenset({"courier", "closer", "auditor", "mechanic"}),
           lambda w, direction=None: w.store.mail(direction=direction)),
        _t("invoice.issue", "Issue a USDC invoice", frozenset({"treasurer"}),
           lambda w, job, income_account="income.labor": invoices.issue(w, job, income_account=income_account)),
        _t("invoice.collect", "Mark invoice paid / settle receivable", frozenset({"treasurer", "mechanic"}),
           lambda w, ref, source="tool": invoices.collect(w, ref, source=source)),
        _t("invoice.list", "List invoices", frozenset({"treasurer", "bookkeeper", "auditor", "director", "mechanic"}),
           lambda w, status=None: w.store.invoices(status)),
        _t("ledger.snapshot", "Balances, trailing revenue, equity", GOV | frozenset({"bookkeeper"}),
           lambda w: w.ledger.snapshot(now=w.now)),
        _t("wallet.public", "Public ETH/SOL addresses", ALL,
           lambda w: w.wallet.public()),
        _t("human.ask", "Request a login/credential from the human", frozenset({"courier", "operator", "hunter", "mechanic"}),
           lambda w, service, instruction, fields, why: w.human.ask(service, instruction, fields, why)),
        _t("brain.complete", "Language model (Claude subscription or sim brain)",
           frozenset({"closer", "crafter", "publisher", "scout", "improver", "director", "auditor", "mechanic"}),
           lambda w, prompt, tier="fast", system="You are a Sovereign agent.": w.router.complete(prompt, tier=tier, system=system)),
        _t("craft.produce", "Write deliverable files in the job jail", frozenset({"crafter"}),
           lambda w, job: produce(w, job)),
        _t("market.certify", "Walk-forward certify trading strategies", frozenset({"risk", "trader", "mechanic"}),
           lambda w: _certify(w)),
        _t("playbook.read", "Read an agent playbook (A/B aware for closer)", ALL,
           lambda w, agent, job_id=None: _read_pb(w, agent, job_id)),
        _t("playbook.write_trial", "Write a trial playbook for A/B", frozenset({"improver"}),
           lambda w, agent, body: _write_trial(w, agent, body)),
        _t("playbook.promote", "Promote trial playbook to control", frozenset({"improver", "auditor"}),
           lambda w, agent: _promote(w, agent)),
        _t("governance.freeze", "Freeze an agent", frozenset({"risk", "ethics", "mechanic", "auditor"}),
           lambda w, agent, reason: w.freeze(agent, reason)),
        _t("governance.thaw", "Thaw a frozen agent", frozenset({"mechanic", "risk", "director"}),
           lambda w, agent, reason: w.thaw(agent, reason)),
        _t("memory.kv_get", "Read kv memory", GOV | frozenset({"bookkeeper"}),
           lambda w, key, default=None: w.store.get_kv(key, default)),
        _t("memory.kv_set", "Write kv memory", GOV,
           lambda w, key, value: (w.store.set_kv(key, value), True)[1]),
        _t("heal.diagnose", "Run engine health checks", frozenset({"mechanic", "director", "auditor"}),
           lambda w: _diagnose(w)),
        _t("heal.repair", "Auto-repair repairable findings", frozenset({"mechanic"}),
           lambda w, full=False: _repair(w, full)),
        _t("offers.list", "Listed productized offers", frozenset({"scout", "publisher", "closer", "director"}),
           lambda w: w.store.offers()),
        _t("files.list_work", "List jailed work files", frozenset({"crafter", "auditor", "mechanic"}),
           lambda w, job_id: _list_work(w, job_id)),
    ]
    for t in tools:
        r.register(t)
    return r


def _reject(w, job_id: str, source: str = "tool"):
    from sovereign.labor.pipeline import reject_job

    return reject_job(w, job_id, source=source)


def _certify(w) -> list[dict[str, Any]]:
    from sovereign.engine.world import ensure_certified, load_prices
    import numpy as np

    w.certified = []
    load_prices(w)
    reports = certify(np.array(w.market_close, dtype=float), w.config.risk)
    w.certified = reports
    w.store.set_kv("certified", reports)
    return reports


def _read_pb(w, agent: str, job_id=None) -> str:
    from sovereign.memory.playbooks import read_playbook_ab

    return read_playbook_ab(w, agent, job_id)


def _write_trial(w, agent: str, body: str) -> str:
    path = w.config.paths().playbooks / f"{agent}.trial.md"
    path.write_text(body)
    return str(path)


def _promote(w, agent: str) -> bool:
    from sovereign.memory.playbooks import promote_trial

    return promote_trial(w.config.paths().playbooks, agent)


def _diagnose(w) -> dict[str, Any]:
    from sovereign.heal.checks import diagnose

    return {"findings": [f.as_dict() for f in diagnose(w)]}


def _repair(w, full: bool = False) -> dict[str, Any]:
    from sovereign.heal.repair import setup

    return setup(w, full=full)


def _list_work(w, job_id: str) -> list[str]:
    p = w.config.paths().work / job_id
    if not p.exists():
        return []
    return [x.name for x in p.iterdir() if x.is_file()]
