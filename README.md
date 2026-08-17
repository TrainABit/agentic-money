"""Sovereign — autonomous multi-agent economic engine.

This is a firm that runs as software. Agents find paid work, ship it, collect
USDC, certify trading strategies, buy infra, and **control each other**. A
human is only asked for logins (Claude subscription, KYC, platform tokens).
There is no approval queue for ordinary work.

Cognition uses a **Claude Pro/Max subscription** via Claude Code
(`claude -p`), not the Anthropic API. Most ticks use no model at all:
bookkeeping, scraping, risk, and trade signals are code. No Anthropic HTTP
fallback is currently implemented; live fallback and token-accounting details
are documented in [`docs/MODELS.md`](docs/MODELS.md).

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
sovereign bootstrap                      # one-shot: init + full repair + readiness verdict
sovereign init
sovereign setup                          # repair paths, playbooks, inbox, lock, tools
sovereign doctor --fix                   # same heal + CLI/wallet checks
sovereign tools --agent mechanic         # permissioned tool bus
sovereign agents --agent closer          # roster: mission, tier, tools, prompt, inbox
sovereign run --ticks 30                 # sim marketplace + paper trading
sovereign serve --mode live              # daemon, file-locked, crash-heals
sovereign backtest --live-data           # certify strategies on public BTC
sovereign dashboard                      # observer: pipeline, invoices, wallets, health
```

The dashboard is unauthenticated only on its default loopback bind. Every
`/api/*` route requires `Authorization: Bearer …` when
`SOVEREIGN_DASHBOARD_TOKEN` is set, and a non-loopback bind is refused without
that variable:

```bash
export SOVEREIGN_DASHBOARD_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
sovereign dashboard --host 0.0.0.0
curl -H "Authorization: Bearer $SOVEREIGN_DASHBOARD_TOKEN" http://127.0.0.1:7474/api/status
```

After the first unauthorized API response, the browser UI shows a password
input, keeps the token in session storage, and sends it only in the
`Authorization` header—not in the URL. Put TLS in front of any non-loopback
deployment; bearer tokens are not transport encryption.

Strategy certification requires at least 480 finite, positive, chronological
daily bars: 400 train bars plus 80 out-of-sample bars. Short histories fail
closed and never use a half-split substitute. `n_trades` counts economic
entries, exits, and direction changes, not volatility-target size adjustments.
`positive_bar_rate` is the share of net-return bars above zero, not a trade win
rate. `round_trip_cost` is the total entry-plus-exit cost, charged one half on
each one-way unit of turnover.

Live market scheduling uses wall-clock time rather than daemon tick counts.
Prices refresh hourly by default. Certification retries hourly only while no
certification report list exists (for example, while live prices are
unavailable); after any report list is stored, normal recertification runs
weekly. That switch is based on report existence, not on a strategy passing:
an all-rejected or insufficient-data report still moves to the weekly cadence.
These defaults are configurable through `live_timing.price_refresh_hours`,
`live_timing.certification_retry_hours`, and
`live_timing.recertify_hours`.

Live labor loop (no auto-pay):

```bash
claude login
sovereign run --mode live --ticks 1000000
# inbound accept:
sovereign accept job_...
# or drop data/mail/inbox/lead.json with subject "job_... ACCEPTED"
# after delivery the engine invoices USDC to the firm wallet
sovereign paid inv_... --confirm         # manual live override after independent verification
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

### Runtime and agent communication

The sixteen agents run inside one supervised engine process against one
SQLite world — not separate VMs. Tool grants derive from each agent's spec
(drift fails at startup), and agents talk over a persistent message bus
(multicast, request/reply with deadlines, at-least-once retries, dead-letter
audit). `sovereign agents` prints the live roster; the full topology, roster
table, message protocol, quorum walkthrough, and scaling path are in
[`docs/RUNTIME.md`](docs/RUNTIME.md).

The firm runs without you. Ordinary work has no approval queue. If sqlite
paths, playbooks, the human inbox, a stale lock, or the tool registry break,
**Mechanic** repairs them every tick. `sovereign setup` / `doctor --fix` do
the same on demand. The daemon heals and continues after a crashed tick.
Frozen agents thaw after a cooldown if reputation recovers. Improver A/B
tests closer playbooks and promotes or reverts from measured USD.

New ETH and Solana accounts are derived from the init mnemonic and encrypted
at rest. Existing encrypted wallet bundles are loaded unchanged. Legacy
random Solana wallets are not derivable from that mnemonic and remain
recoverable only from their `sol_secret` in `data/secrets.enc` (using
`data/master.key`) until explicitly migrated; preserve both files. Credentials
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

Optional validated overrides can be placed in `data/config.yaml`; explicit
`--mode` and `--data-dir` CLI arguments take precedence. A human or email
claim that an invoice is paid never settles it—live manual settlement requires
`sovereign paid ... --confirm`.
"""
