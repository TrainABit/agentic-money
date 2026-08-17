"""Sovereign — autonomous multi-agent economic engine.

This is a firm that runs as software. Agents find paid work, ship it, collect
USDC, certify trading strategies, buy infra, and **control each other**. A
human is only asked for logins (Claude subscription, KYC, platform tokens).
There is no approval queue for ordinary work.

Cognition uses a **Claude Pro/Max subscription** via Claude Code
(`claude -p`), not the Anthropic API. Most ticks use no model at all:
bookkeeping, scraping, risk, and trade signals are code.

## Targets

| Minimum | Recommended | Good |
| --- | --- | --- |
| $2,000 / mo | $5,000 / mo | $7,000 / mo |

Labor cashflow is the path to $2k. Retainers/products are the path to $5–7k.
Trading compounds a treasury; it does not pay rent from a tiny bankroll.

Read the full plan: [`docs/PLAN.md`](docs/PLAN.md) · plays: [`docs/PLAYS.md`](docs/PLAYS.md) · bootstrap: [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md)

## Quick start

```bash
pip install -e ".[dev]"
sovereign init
sovereign setup                          # repair paths, playbooks, inbox, lock, tools
sovereign doctor --fix                   # same heal + CLI/wallet checks
sovereign tools --agent mechanic         # permissioned tool bus
sovereign run --ticks 30                 # sim marketplace + paper trading
sovereign serve --mode live              # daemon, file-locked, crash-heals
sovereign backtest --live-data           # certify strategies on public BTC
sovereign dashboard                      # observer: pipeline, invoices, wallets, health
```

Live labor loop (no auto-pay):

```bash
claude login
sovereign run --mode live --ticks 1000000
# inbound accept:
sovereign accept job_...
# or drop data/mail/inbox/lead.json with subject "job_... ACCEPTED"
# after delivery the engine invoices USDC to the firm wallet
sovereign paid inv_...                   # or it marks paid when USDC arrives
```

## What is running

```
Mechanic (first) → diagnose/repair/thaw → other agents keep working
Director → funds plays by trailing-30d ROI
Treasurer / Risk / Auditor / Ethics / Improver → mutual control
Hunter → Closer → mail outbox → accept → Crafter (real files, Claude jail) → invoice → USDC
Trader → certified TSMOM only, walled book, circuit breakers
Publisher / Scout / Operator / Courier / Bookkeeper
```

Every agent calls a **permissioned tool bus** (`jobs.*`, `mail.*`, `invoice.*`,
`heal.*`, `playbook.*`, `governance.freeze/thaw`, `brain.complete`). A hunter
cannot freeze anyone. A closer cannot run `heal.repair`. Denials are audited;
tool kwargs (secrets) are not logged.

The firm runs without you. Ordinary work has no approval queue. If sqlite
paths, playbooks, the human inbox, a stale lock, or the tool registry break,
**Mechanic** repairs them every tick. `sovereign setup` / `doctor --fix` do
the same on demand. The daemon heals and continues after a crashed tick.
Frozen agents thaw after a cooldown if reputation recovers. Improver A/B
tests closer playbooks and promotes or reverts from measured USD.

Wallets (ETH + SOL) are generated at init and encrypted at rest. Credentials
injected via `sovereign reply` land in the same vault; after consume they are
scrubbed from `human_inbox.json` and never written to the event log or
`human_replies.json`. Use `KEY=-` to read a secret from stdin (not argv).
`data/master.key` sits next to `secrets.enc` — encryption-at-rest only helps
if the key is copied separately; prefer filesystem permissions. Mnemonic
reveal requires `SOVEREIGN_CONFIRM_REVEAL=1`.

The Claude job crafter is jailed with `Path.relative_to` (not a string prefix)
and Claude Code allowlists (`Read/Write/Edit/Glob/Grep`; no Bash). Job-board
text is wrapped as untrusted data. Revenue is recognized when USDC is
collected, not when an invoice is issued. Live listings without a contact
email become `needs_channel` instead of mailing `client@unknown.local`.

Sim revenue is a closed marketplace used to prove the loop (invoices still
exist; they autocollect). Live revenue requires real clients: proposals go
to `data/mail/`, jobs stay `applied` until mail/`sovereign accept`, invoices
stay open until chain watch or `sovereign paid`. Other plays keep running
while a login sits in the inbox.
"""
