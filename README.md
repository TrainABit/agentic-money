# Sovereign

> An autonomous multi-agent engine that runs a small software business: it finds paid work, delivers it, invoices, verifies payment on-chain and keeps its own books. Sixteen agents check and govern each other instead of queuing every step for a human.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Tests: 462 passing](https://img.shields.io/badge/tests-462%20passing-brightgreen.svg)
![Coverage gate: 82%](https://img.shields.io/badge/coverage%20gate-82%25-green.svg)

Most "agent" demos are one LLM in a loop. Sovereign is the opposite: **mostly deterministic code with an LLM only where judgment is needed**. Bookkeeping, scraping, risk checks and trade signals are plain Python. The model (Claude via Claude Code) is called for writing proposals and building deliverables, and that call runs in a jail.

The interesting problems here are not prompting. They are **money correctness, permissions, fault isolation and governance** when software acts on its own.

---

## Highlights

- **16 agents with explicit permissions.** Every agent is an `AgentSpec` with a mission, a model tier and a tool allowlist. An agent physically cannot call a tool it was not granted. Denials are audited, and spec drift fails at startup.
- **Checks and balances instead of an approval queue.** Risk, ethics and the auditor can freeze other agents. Reputation scales autonomy. High-impact actions (e.g. buying infrastructure) need a multi-seat quorum recorded in the database.
- **Money is treated like money.** Integer cents at the store boundary, ledger invariants with property-based tests (Hypothesis), and invoices that change state only inside transactions. A human saying "I paid" never settles an invoice. Live settlement matches **on-chain USDC transfer logs** (Ethereum `eth_getLogs`, Solana signatures) by amount, sender and txid, with dedup.
- **Jailed LLM execution.** Live crafting runs `claude -p` as a subprocess confined to the job's work directory, with tools limited to `Read/Write/Edit/Glob/Grep` (no shell, no network), bounded output and a timeout. It fails closed: in live mode it never silently falls back to a simulated brain.
- **Self-healing daemon.** A single supervised process with a file lock, a heartbeat that runs the agents in a fixed order, a per-agent watchdog timeout, heal-and-continue after a crashed tick, and P0/P1 alerts by mail or webhook.
- **Operable.** Versioned schema migrations, online SQLite backups with a restore drill that never touches live data, encrypted secrets with key rotation (file or OS keyring), a Docker image, a systemd unit and health-check probes.
- **Simulation first.** A simulated marketplace and a paper broker let the whole firm run deterministically in tests and in `sovereign run` before anything goes live. Trading strategies must pass a backtest ("certification") before the trader may use them.

## Architecture

```
sovereign serve  (one process, file-locked, heartbeat)
  │
  ├─ mechanic ─────────── health checks, repair, re-certify        (runs first)
  ├─ bookkeeper · risk · ethics · director                          oversight
  ├─ hunter → closer → crafter → treasurer                          labor pipeline
  ├─ trader · publisher · scout · operator                          capital & products
  ├─ auditor · improver                                             quality & learning
  └─ courier ──────────── the only interface to the human           (runs last)
        │
        ▼
  SQLite world (WAL)  ledger · jobs · invoices · mail · votes · events
                      messages (durable comms bus) · knowledge (FTS5)
```

| Agent | Mission |
|---|---|
| **hunter** | Find real, winnable jobs that fit the firm's skills |
| **closer** | Turn jobs into accepted work with short, honest, fixed-price proposals |
| **crafter** | Build and ship the actual deliverable (files and a runbook) |
| **treasurer** | Invoice delivered work, settle only on verified payment |
| **bookkeeper** | Snapshot balances and revenue every tick so all decisions use the same numbers |
| **director** | Allocate attention and budget by measured return |
| **risk** | Enforce loss limits, halt trading, freeze agents before damage compounds |
| **ethics** | No leaked secrets, no false claims, no spam; freeze offenders |
| **auditor** | Sample work and books, penalise empty output, reward what is provably real |
| **improver** | A/B test playbooks, then promote or revert |
| **trader** | Execute only certified strategies inside risk caps |
| **publisher** | Package shipped work into reusable products |
| **scout** | Keep a priced offer catalog, propose the next experiment |
| **operator** | Buy compute only when quorum approves a proven need |
| **mechanic** | Keep the engine healthy without being asked |
| **courier** | Route login requests and decisions to the human |

Agents are Python functions over a shared world, not containers. That is a deliberate trade: deterministic replay, one transactional ledger and crash recovery in one place. The durable message bus is already the seam for per-agent worker processes later. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/RUNTIME.md](docs/RUNTIME.md).

## Quick start

```bash
pip install -e ".[dev]"
sovereign bootstrap              # init, full repair, readiness verdict
sovereign run --ticks 30         # simulated marketplace + paper trading
sovereign dashboard              # observer: pipeline, invoices, wallets, health
```

<details>
<summary>More commands</summary>

```bash
sovereign agents --agent closer          # roster: mission, tier, tools, prompt, inbox
sovereign tools --agent mechanic         # permissioned tool bus
sovereign doctor --fix                   # heal + CLI/wallet checks
sovereign serve --mode live              # daemon; refuses unready live starts
sovereign backtest --live-data           # certify strategies on public BTC data
sovereign comms --status dead            # inspect the message bus; --requeue / --purge-days
sovereign backup --restore-drill /tmp/d  # backup + verify + read-only probe
sovereign migrate                        # apply schema versions
sovereign rotate-key --confirm           # re-encrypt secrets; --to-keyring moves custody
sovereign healthcheck                    # Docker/K8s probe
```

Optional extras: `[mail]` (live email channel), `[web]` (Playwright browser automation), `[mcp]` (MCP tool servers), `[keyring]` (OS keyring custody).

</details>

Docker and systemd: [docs/DEPLOY.md](docs/DEPLOY.md). Operations: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Testing

```bash
pytest                           # 462 tests, ~35 s, fully offline
```

The suite covers ledger invariants (property-based), on-chain settlement matching, migrations, fault isolation, the Claude jail, web-automation hardening, governance and a bounded simulation soak. CI runs on Python 3.11 and 3.12 with a coverage floor of 82%, builds the wheel and smoke-tests the import.

## Status and honest gaps

Sovereign runs end to end in simulation and paper-trading mode. **Not built yet**, and not to be inferred from the docs:

- per-agent worker processes (the bus is ready, the supervisor is not)
- live exchange order placement (paper broker and certified signals only)
- fiat rails (Stripe invoicing, refunds, chargebacks)
- site-specific adapters for job platforms
- key management beyond the OS keyring

## Documentation

[Architecture](docs/ARCHITECTURE.md) · [Runtime](docs/RUNTIME.md) · [Models](docs/MODELS.md) · [MCP](docs/MCP.md) · [Web automation](docs/WEB.md) · [Plan](docs/PLAN.md) · [Plays](docs/PLAYS.md) · [Bootstrap](docs/BOOTSTRAP.md) · [Deploy](docs/DEPLOY.md) · [Runbook](docs/RUNBOOK.md)

## How this was built

I designed Sovereign and wrote its specs, safety rules and acceptance criteria. The code was written by Cursor background agents working through those specs, which is why the commits are authored by "Cursor Agent". I reviewed the work, directed the audits and decided what went in.
