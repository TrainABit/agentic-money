# Runtime — how the multi-agent firm actually executes

This is the reference for what "sixteen agents" physically means in this
codebase: one process, one database, one message bus, and spec-derived
permissions. Verify any of it with `sovereign agents`, `sovereign tools`,
and `sovereign bootstrap`.

## Topology today

The firm is **one supervised engine process**, not a fleet.

- `sovereign run` / `sovereign serve` execute a heartbeat
  (`sovereign/engine/heartbeat.py`). Each tick calls the sixteen agent
  functions in a **fixed, deterministic order** (mechanic first, courier
  last). The daemon takes a file lock (`data/engine.lock`) so two hearts
  cannot beat against one database, runs a full heal on start, and heals and
  continues after a crashed tick.
- All state lives in a **shared SQLite world** (`data/sovereign.db`, WAL
  mode): ledger, jobs, invoices, mail, offers, missions, votes, outcomes,
  kv, events, and the `messages` table the comms bus persists to. An agent
  "acting" is a Python function reading and writing that world through the
  tool registry.
- Agents are **not separate VMs, containers, or processes**. That is a
  deliberate trade: deterministic sim replay, transactional consistency on
  one ledger, crash-heal in one place, and zero infrastructure spend until
  quorum approves buying any (see the operator example below).

Isolation still exists where it matters, just not at the process boundary:

- **Claude subprocess jail** — the only untrusted-code-adjacent work
  (live crafting) runs `claude -p` as a subprocess jailed to the job's work
  directory via `Path.relative_to` containment, with tools allowlisted to
  `Read/Write/Edit/Glob/Grep` (no Bash, no network tools), bounded output,
  and a timeout (`sovereign/runtime/router.py`).
- **Tool permissions** — every agent action goes through the permissioned
  registry; an agent physically cannot call a tool its spec does not grant,
  and denials are audited as `tool_denied` events.
- **Freeze / quorum governance** — risk, ethics, and the auditor can freeze
  an agent (its function is skipped each tick); reputation scales autonomy;
  high-impact actions need multi-seat quorum recorded in the `votes` table.

## Agent roster

Generated from `sovereign.agents.spec.AGENT_SPECS`, the single source of
truth (`sovereign agents` prints the live version, including tool grants and
queued inbox depth; `--agent NAME` adds the full system prompt).

| Agent | Mission | Tier | Message kinds handled |
| --- | --- | --- | --- |
| `mechanic` | Keep the engine healthy so every other agent keeps earning: diagnose, repair, re-certify, and thaw without being asked. | fast | `ping`, `notify` |
| `bookkeeper` | Keep the books legible: snapshot balances and trailing revenue every tick so every decision uses the same numbers. | none (deterministic) | `ping`, `notify` |
| `risk` | Enforce the loss limits: halt trading, wall operating cash, and freeze agents before damage compounds. | none (deterministic) | `ping`, `notify`, `vote_request` |
| `ethics` | Police conduct: no leaked secrets, no false claims, no spray — freeze offenders and say exactly why. | none (deterministic) | `ping`, `notify` |
| `director` | Allocate attention and budget across plays by measured return, protecting the $2,000 trailing minimum above all. | think | `ping`, `notify`, `vote_request` |
| `hunter` | Fill the top of the pipeline with real, winnable jobs that fit the firm's skills. | none (deterministic) | `ping`, `notify` |
| `closer` | Turn open jobs into accepted work with short, specific, honest proposals at fixed prices. | work | `ping`, `notify` |
| `crafter` | Build and ship the actual deliverable for each accepted job: real files, a runbook, no theatre. | work | `ping`, `notify` |
| `trader` | Execute only certified strategies inside risk caps; the code decides entries and you never improvise. | none (deterministic) | `ping`, `notify` |
| `publisher` | Package real, shipped work into listed products that can sell again without new labor. | work | `ping`, `notify` |
| `scout` | Keep a small, priced offer catalog and propose the next experiment the numbers justify. | fast | `ping`, `notify` |
| `operator` | Run the firm's infrastructure frugally, buying compute only when quorum approves a proven need. | none (deterministic) | `ping`, `notify` |
| `treasurer` | Issue invoices for delivered work, settle them only on verified payment, and keep operating cash walled. | none (deterministic) | `ping`, `notify`, `vote_request` |
| `auditor` | Sample the firm's work and books, slash empty or dishonest output, and boost what is provably real. | fast | `ping`, `notify` |
| `improver` | Turn measured outcomes into better playbooks: trial, A/B, then promote or revert — never edit control directly. | work | `ping`, `notify` |
| `courier` | Be the firm's interface to the human: route login requests and authorized decisions without becoming an approval bottleneck. | none (deterministic) | `ping`, `notify` |

Tier `none (deterministic)` means the agent's tick is pure code and never
calls a model. Only `treasurer`, `risk`, and `director` handle
`vote_request`; every other spec pins "you hold no vote" into the prompt.

## Tool permissions

- **Source of truth: `AgentSpec.tools`** in `sovereign/agents/spec.py`. Each
  spec also embeds the same tool list into the agent's fixed system prompt;
  a regex cross-check at import time raises if the prompt's tool section
  drifts from the grants.
- **The registry is derived, never hand-edited.** `build_registry()`
  (`sovereign/tools/catalog.py`) takes its allowlists from
  `tool_matrix()` — the spec-derived map of tool → agents.
- **Drift fails at startup.** `validate_matrix` raises `RuntimeError` during
  `bootstrap()` if a spec references a tool missing from the catalog, a
  catalog tool is granted to no spec, or any allowlist is empty. A running
  world with mismatched prompts and enforcement cannot exist.
- Every agent holds the two universal tools (`wallet.public`,
  `playbook.read`). Everything else is per-spec: a hunter cannot freeze, a
  closer cannot repair, a mechanic cannot settle invoices. Denials emit
  `tool_denied` events; tool kwargs (which may hold credentials) are never
  written to the event log. The `readiness()` check `tools_and_specs`
  re-verifies registry/spec agreement for all sixteen agents.

## Message protocol

Agent-to-agent messaging is the persistent bus in `sovereign/comms/bus.py`,
stored in the `messages` table of the same SQLite world.

**Message fields:** `sender`, `recipient` (one row per recipient),
`kind`, `payload`, `thread_id`, `correlation_id`, `reply_to`,
`expects_reply`, `deadline`, `attempts` / `max_attempts`, plus `status` and
a last `error`. Kinds match `^[a-z][a-z0-9_.]{0,63}$`; payloads are JSON
dicts capped at 32 KiB; senders and recipients must be roster names.

**Statuses:** `queued` → `done` (acked), `expired` (deadline passed), or
`dead` (dead-lettered).

**Delivery semantics:**

- *Per-recipient rows.* A multicast `send(sender, [r1, r2, ...], ...)`
  writes one row per (deduplicated) recipient in a single transaction; all
  rows share one `thread_id` and `correlation_id`, so fan-outs can be joined
  back together.
- *At-least-once with handler retries.* A recipient reads its `inbox`
  (oldest first) and `ack`s each handled message to `done`. A failing
  handler calls `fail(message, error)`, which increments `attempts` and
  leaves the row queued for retry until `max_attempts` (default 3, ceiling
  10), then marks it `dead`.
- *Dead letters are audited.* Dead-lettering emits a `comms_dead_letter`
  event carrying ids and kinds only — never payload contents. Dead letters
  flip the `comms` health finding, which the mechanic sees in `diagnose()`
  and `sovereign bootstrap` surfaces via `engine_health` / `comms_backlog`.
- *Broadcast* sends to the whole roster minus the sender. *Request/reply*:
  `request(...)` requires a deadline and sets `expects_reply`;
  `reply(original, ...)` joins the original thread and correlation, records
  `reply_to`, and defaults the kind to `<kind>.reply` (replying to a reply
  is rejected, and only the original recipient may reply). *Gather*:
  `replies(correlation_id)` collects answers to a fan-out and
  `outstanding(correlation_id)` counts request rows still awaiting an ack.
- *Simulated-time deadlines.* Every time-dependent entry point takes an
  explicit timezone-aware `now`; the bus never reads the wall clock, so sim
  and live behave identically. `inbox` hides past-deadline rows even before
  an `expire_due` sweep marks them `expired`.

The heartbeat processes inboxes each tick: expired deadlines are swept and
queued messages are handed to their recipients' handlers in agent order,
acked on success, failed (and eventually dead-lettered) on error.

**Operations.** `sovereign comms` lists sanitized message rows (payloads are
never printed), `--requeue MSG_ID` returns a dead or expired row to the
queue with reset attempts, and `--purge-days N` deletes old `done`/`expired`
rows. The heartbeat also runs an automatic retention sweep (14-day horizon,
daily in live, every 50 ticks in sim). `/api/metrics` and
`/api/comms?status=dead` expose the same data read-only on the dashboard,
and the weekly report written to `data/artifacts/reports/` summarizes comms
health next to revenue, pipeline, incidents, and goal progress.

## Mail transports

Outbound mail resolves the first configured transport: **AgentMail**
(vault credentials `AGENTMAIL_API_KEY` + `AGENTMAIL_INBOX_ID`; optional
dependency installed via `pip install -e ".[mail]"`), then **SMTP**
(`SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`SMTP_FROM`), then the
**local file outbox** (`data/mail/outbox/`, also the durable queue when a
remote transport fails — the message keeps its idempotency key and the send
error). With AgentMail configured, the courier polls the inbox on the
`live_timing.mail_poll_minutes` cadence, labels fetched messages
`sovereign-processed`, deduplicates by message id, and writes each new
message as an `am_<id>.json` drop-in. Drop-ins flow through the same
`ingest_dropins` → `authorize_state_change` pipeline as file leads:
an accept/reject only lands when the sender matches the job contact, a
trusted sender, or a valid HMAC signature. Inbound bodies are untrusted
data end to end.

## Sandbox

Live crafting already runs Claude jailed to the job directory with a tool
allowlist. When bubblewrap is installed, the subprocess is additionally
wrapped in an OS-level filesystem sandbox (`models.sandbox: auto`, the
default): the entire filesystem is bind-mounted read-only, only the job's
work directory and the CLI's own session state are writable, and `/tmp` is
a private tmpfs. Network stays shared because the CLI must reach its model
provider. `models.sandbox: bwrap` makes the sandbox mandatory and fails
closed (the craft queues) when bubblewrap is missing; `off` disables it.

## Worked example: operator infra purchase quorum

The operator wants a $6/month VPS (the standing plan in
`sovereign/agents/roles.py`). Buying infrastructure requires
treasurer-plus-director quorum (`Council.REQUIRED["buy_infra"]`).

1. **Request fan-out.** The operator issues one
   `request("operator", ["treasurer", "risk", "director"], "vote_request",
   {...plan, cost, account...}, deadline=...)`. That writes three queued
   rows sharing one `thread_id`/`correlation_id`, each with
   `expects_reply` set and the same deadline.
2. **Per-seat policy votes.** Each voting seat handles `vote_request`
   strictly by the policy pinned in its spec prompt: the treasurer says yes
   only if the spend leaves operating cash above the floor, touches no
   walled trading funds, and names its expense account; risk says yes only
   if every limit stays intact (no active halt, operating cash walled,
   position caps respected); the director says yes only if the spend fits a
   funded play's budget and the $2,000 trailing minimum stays protected.
   Each seat `reply`s yes/no with a one-line reason and acks its request
   row. (In today's deterministic tick the same policies run as code:
   `council.auto_votes_for_spend` plus the director's trailing-revenue
   vote.)
3. **Quorum record.** The operator gathers `replies(correlation_id)` and
   calls `council.quorum(action_id, "buy_infra", votes)`. Quorum passes only
   with a yes from **both** treasurer and director; every cast vote —
   including risk's — is recorded in the `votes` table with the action id.
   Only then does the treasury pay and book to `expenses.infra` (live
   purchases additionally require a provider token and
   `allow_live_infra_buy`).
4. **Expiry path.** A seat that never answers is not an approval. At the
   deadline the unanswered request rows vanish from inboxes and an
   `expire_due` sweep marks them `expired`; `outstanding()` drops to zero
   without the missing reply, quorum fails closed with
   "missing yes from <seat>", and the operator drops the plan until the
   numbers change. Nothing is bought on silence.

## Scaling path

The contracts are designed so the topology can change without rewriting the
firm:

- **The bus is already the boundary.** Messages are durable, per-recipient,
  at-least-once, deadline-scoped rows — exactly the contract a separate
  worker process or container per agent would consume. A future worker for
  agent X polls `inbox("X")`, acks, fails, and replies over the same
  protocol; senders never know whether the recipient is a function call in
  the same tick or a process on another box.
- **The spec is already the identity.** `AgentSpec` pins name, mission,
  tier, tool grants, handled kinds, and the full system prompt. A packaged
  per-agent worker ships with its spec; the registry (or a service fronting
  it) keeps enforcing the same spec-derived tool matrix, so permissions do
  not loosen when processes split.
- **What changes:** agent order becomes concurrency (the bus's explicit
  `now`, deadlines, and retries already assume delivery is not
  instantaneous); SQLite becomes a served store or a different backend
  behind the same `Store` interface; tool calls become RPC to a registry
  service with the same allowlists and audit events.
- **What must NOT be shared out to per-agent workers:** the wallet key
  material (the mnemonic and private keys in `data/secrets.enc`) and the
  master key (`data/master.key`) that decrypts it. Signing and settlement
  stay with a single treasury boundary; workers keep receiving only
  `wallet.public`, exactly as the tool matrix grants today. Per-job work
  jails likewise stay private to the worker that owns them.
