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
sovereign doctor
sovereign run --ticks 30                 # sim marketplace + paper trading
sovereign backtest --live-data           # certify strategies on public BTC
sovereign dashboard                      # read-only observer
```

Live cognition (after you run `claude login` on the host):

```bash
sovereign run --mode live --ticks 1000000
```

## What is running

```
Director → funds plays
Treasurer / Risk / Auditor / Improver → mutual control
Hunter → Closer → Crafter → collect USDC
Trader → certified TSMOM only, walled book, circuit breakers
Publisher / Scout / Operator / Courier / Bookkeeper
```

Wallets (ETH + SOL) are generated at init and encrypted at rest. The mnemonic
is never logged. Reveal requires `SOVEREIGN_CONFIRM_REVEAL=1`.

Sim revenue is a closed marketplace used to prove the loop. Live revenue
requires real clients and a Claude login. The engine will keep working on
every play that is not blocked by a missing login.
"""
