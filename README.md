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

Read the full plan: [`docs/PLAN.md`](docs/PLAN.md) · plays: [`docs/PLAYS.md`](docs/PLAYS.md) · bootstrap: [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md) · architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · runbook: [`docs/RUNBOOK.md`](docs/RUNBOOK.md) · deploy: [`docs/DEPLOY.md`](docs/DEPLOY.md) · trading: [`docs/TRADING.md`](docs/TRADING.md)

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
sovereign serve --mode live              # daemon; refuses unready live starts (--force overrides)
sovereign serve --workers                # same loop, eligible agents in spawned processes
sovereign trading                        # Hyperliquid/paper venue status (never keys)
sovereign worker --agent bookkeeper --once
sovereign backtest --live-data           # certify strategies on public BTC
sovereign dashboard                      # observer: pipeline, invoices, wallets, health, metrics
sovereign comms --status dead            # inspect bus messages; --requeue / --purge-days
sovereign backup --out /path/backup      # online SQLite backup + manifest; --verify checks it
sovereign backup --restore-drill /tmp/drill  # backup + verify + read-only probe (never restores live)
sovereign healthcheck                    # Docker/K8s exec probe; --stale-seconds for liveness
sovereign migrate                        # apply schema versions; print schema_log
sovereign maintain                       # prune events/comms and VACUUM
sovereign rotate-key --confirm           # re-encrypt secrets.enc; --to-keyring moves custody
sovereign debug --ticks 3                # traced ticks: slowest tools/agents, comms, errors
```

Packaging: `docker build -t sovereign .` (see [`docs/DEPLOY.md`](docs/DEPLOY.md)),
`deploy/docker-compose.yml`, and `deploy/sovereign.service`. The image and
the unit treat `sovereign healthcheck` as the probe.

Master-key custody defaults to `data/master.key`. Set
`wallet.master_key_backend: keyring` (and `pip install -e ".[keyring]"`)
to keep the Fernet key out of the data directory. Rotate with
`sovereign rotate-key --confirm` while the daemon is stopped.

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

Outbound mail picks the first configured transport: AgentMail (vault
credentials `AGENTMAIL_API_KEY` + `AGENTMAIL_INBOX_ID`, installed with
`pip install -e ".[mail]"`), then SMTP (`SMTP_HOST` …), then the local file
outbox. With AgentMail configured, the courier also polls the inbox on a
`live_timing.mail_poll_minutes` cadence and turns new messages into the same
authorized drop-in pipeline used for file leads — sender authorization still
gates every accept/reject, and a transport failure queues the message with
the error instead of losing it.

### Operating real websites

With the `[web]` extra (`playwright` + `playwright install chromium`) and an
explicit opt-in, hunter/closer/operator/courier can operate real websites
headlessly — navigate, log in via vaulted sessions, click, type, upload,
download, and extract — with no desktop. `web.enabled` defaults to false and
the domain allowlist is empty, so everything web fails closed until the
operator turns it on in `data/config.yaml`. CAPTCHAs, 2FA, and first-time
logins are hard human boundaries: the engine files one `web:<service>`
request and the human completes it once via `sovereign web-login <domain>
--import state.json` (or `--headful` on a machine with a display), after
which the encrypted per-site session is reused. Sessions are sealed with the
wallet master key under `data/web_sessions/`, credentials are typed only by
ALLCAPS vault reference and never logged or echoed, all page content reaches
agents fenced as untrusted data, and browser work is bounded by per-session
and per-tick action caps, started lazily, and closed at tick end. The
mandate still applies on the web: no fraud, spam, manipulation, or KYC
evasion. Details: [`docs/WEB.md`](docs/WEB.md).

Agents keep a searchable knowledge base (SQLite FTS5, LIKE fallback) with
per-agent namespaces plus a shared `firm` namespace: the closer recalls
won/lost-job lessons into its proposal prompts, the crafter/treasurer/trader
record deliveries, payments, and fills, and governance agents can share firm
lessons. Notes are size-capped, deduplicated, LRU-pruned at 500 per agent,
and always injected into prompts as delimited untrusted data.

Debugging is built in: every tool call is timed (slow calls emit `tool_slow`
events), `SOVEREIGN_DEBUG=1` or `debug.enabled` writes per-tick JSONL traces
with per-agent timings and full traceback tails (trace files only — never
the event log), and `sovereign debug --ticks N` prints hotspots. The engine
stays light by design: inbox processing is gated on one queued-count query
per tick, the live daemon sleeps `idle_tick_seconds` (default 60s) when
there is no work, deep SQLite integrity checks run only on full heals, and
the dashboard pauses polling in hidden tabs.

Jailed live crafting is additionally wrapped in a bubblewrap filesystem
sandbox when `bwrap` is installed (`models.sandbox: auto`, the default):
the whole filesystem is read-only except the job's work directory and the
Claude session state, with a private `/tmp`. Set `models.sandbox: bwrap`
to make the sandbox mandatory (fails closed without bubblewrap) or `off`
to disable.

### Money-making integrations (MCP) and design

The crafter ships **design deliverables** with no external service: a
keyword-routed branch generates a deterministic brand kit (logo SVG, social
card, a self-contained landing page, and a brand guide) that the
`design_studio` play sells. Install nothing for this — it is offline and
built in.

Agents can also use **Model Context Protocol (MCP) servers** as new
money-making tools. With the `[mcp]` extra and an opt-in
(`mcp.enabled: true` in `data/config.yaml`), hunter/closer/crafter/publisher/
scout/operator gain `mcp.list` and `mcp.call`, which route to operator-
configured servers — image generation for richer design work, web
search/research for briefs and lead-gen, code hosting for dev deliverables,
payment rails, data/spreadsheets, and more. Every server declares
`allow_agents`, an optional `allowed_tools` allowlist, and a per-tick call
cap; server secrets come from the encrypted vault by reference and are never
logged; results reach agents fenced as untrusted data; and the registry is
lazy and closed at tick end. `sovereign mcp` lists configured servers and
`sovereign mcp --probe` discovers their tools. The recommended catalog of
money-making servers and config examples: [`docs/MCP.md`](docs/MCP.md).

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
if the key is copied separately; prefer filesystem permissions or the
keyring backend. Mnemonic reveal requires `SOVEREIGN_CONFIRM_REVEAL=1`.
Schema changes apply on open (`PRAGMA user_version`); `sovereign migrate`
is the operator view. `sovereign maintain` is the retention/vacuum path.

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
