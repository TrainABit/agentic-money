# Architecture

Sovereign is one supervised engine process, one SQLite world, and sixteen
permissioned agent functions. This document is the map: what exists, what
the two codebases are, and what is deliberately not built yet.

## Two products, one repository

| Track | Language | Role |
| --- | --- | --- |
| **Sovereign** (`sovereign/`) | Python 3.11+ | Autonomous multi-agent economic engine: labor loop, USDC settlement, paper/live trading, governance. |
| **Agentic Money** (TypeScript app on `cursor/typescript-audit-fixes-8c27`) | TypeScript | Personal-finance dashboard: transactions, budgets, categorization. |

They do not share a database, process, or API. Do not treat the TypeScript
app as the engine's UI — the engine's observer is `sovereign dashboard`.
Consolidating them is a product decision, not a missing import.

## Process topology

```
sovereign serve [--workers]
  └── heartbeat tick
        ├── mechanic (always in-process)
        ├── bookkeeper / risk+ethics / director
        ├── hunter → closer → crafter
        ├── trader / publisher+scout+operator
        ├── treasurer+auditor+improver
        └── courier (typically in-process)
```

- One `FileLock` (`data/engine.lock`) so two hearts cannot beat on one DB.
- Agents are Python functions over a shared `World`, not VMs or containers.
- Isolation is the tool registry, the Claude craft jail, freezes/quorum,
  and a per-agent watchdog timeout — not a desktop per role.
- `workers.enabled` (or `sovereign serve --workers`) may spawn processes
  for agents not in `workers.in_process`. Children reopen SQLite; they
  never pickle `World` and never call `start_tick` / `finish_tick`.
  See [`RUNTIME.md`](RUNTIME.md) and [`TRADING.md`](TRADING.md).

## Persistence

`data/sovereign.db` (WAL, `BEGIN IMMEDIATE` transactions, process `RLock`):

- ledger, jobs, invoices, mail, offers, missions, votes, outcomes, kv, events
- `messages` (comms bus)
- `knowledge` (+ optional FTS5)
- `chain_txids` (on-chain settlement dedup)
- `schema_log` (applied migration versions)

Schema is versioned with `PRAGMA user_version`. Opening a `Store` applies
pending migrations. `sovereign migrate` prints the version;
`sovereign maintain` prunes and vacuums.

Encrypted secrets live in `data/secrets.enc`. The Fernet master key is
either `data/master.key` (default) or the OS keyring
(`wallet.master_key_backend: keyring`). Backups never include `master.key`.

## Money and settlement

- All USD values are integer cents at the store boundary.
- Invoices are issued/collected/voided inside `store.transaction()`.
- Live settlement prefers on-chain USDC transfer logs (ETH `eth_getLogs`,
  Solana signatures) matched by amount + sender + txid. Balance-delta
  matching is the fallback only.
- A human "I paid" claim does not settle. Live manual settle is
  `sovereign paid … --confirm` after independent verification.

## Cognition and tools

- Default live provider: Claude Code CLI, jailed for craft, fail-closed
  (never silent SimBrain in live). Optional HTTP API fallback when
  `models.provider: api` or `allow_api_fallback` is set.
- Tool grants come from `AgentSpec` (`sovereign/agents/spec.py`). Drift
  fails at import/startup.
- Optional extras: `[mail]`, `[web]`, `[mcp]`, `[keyring]`, `[hyperliquid]`.
- Live venue is Hyperliquid only (fail-closed; sim stays paper). See
  [`TRADING.md`](TRADING.md).

## What this wave added

- Versioned schema migrations + `schema_log` + `maintain` / restore-drill
- OS-keyring master-key custody and rotation
- Container image, compose stack, systemd unit, `healthcheck` probe
- Coverage gate, advisory security scans, gated web/MCP IT, mypy subset
- Property tests on the ledger and a bounded sim soak

## Honest remaining gaps

These are not implemented here and should not be inferred from the docs:

- Fiat rails (Stripe invoicing, refunds, chargebacks)
- Site-specific adapters (Upwork/LinkedIn portals)
- KMS beyond the OS keyring
- Merging the TypeScript personal-finance app into this engine
