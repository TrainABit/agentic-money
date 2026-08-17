# Sovereign — Plan for an Autonomous Money-Making Engine

This is the operating plan for the engine in this repo. It is written so the
system can execute it without a human in the loop. Humans appear in one place
only: **credential injection** (KYC, OAuth, 2FA, platform logins the engine
cannot create). Agents review, freeze, and replace each other.

Targets (USD, trailing 30-day recognized revenue, net of direct fees):

| Label | Monthly | What it means operationally |
| --- | --- | --- |
| Minimum | $2,000 | Survival. Labor studio is live. Treasury > $0. |
| Recommended | $5,000 | One retainer + productized delivery + small treasury yield. |
| Good | $7,000 | Two retainers or a productized pipeline + compounding book. |

Trading is **not** the path to $2k from a small bankroll. Labor cashflow is.
Trading and products compound what labor earns. The engine is honest about that
and still runs all three in parallel.

---

## 1. Thesis

An autonomous firm can sell **work**, **systems**, and **risk** under one
treasury.

- **Work** (fastest cash): find paid tasks, do them, invoice, collect.
- **Systems** (best scaling): turn repeated work into products and retainers.
- **Risk** (compounding): only deploy certified, vol-targeted strategies on
  money the Treasurer has explicitly allocated. Never rent-seek the operating
  account.

The legal person is still a human (you). Agents act **in your name** with a
written mandate: they may bind the firm within policy, spend within budgets,
and sign delivery. They may not commit fraud, spam, market manipulation, or
KYC evasion. The Ethics/Risk pair can freeze any agent. There is no human
approval gate for ordinary work.

---

## 2. Entity, money, identity ("own everything")

Crypto-first. Fiat is an off-ramp, not a bootstrap blocker.

| Asset | Owner | How it is created | Human needed? |
| --- | --- | --- | --- |
| Engine name + mandate | Firm | `sovereign init` | No |
| ETH + SOL wallets | Firm | Generated, encrypted at rest | No |
| USDC treasury | Firm | Receive to hot wallet | No |
| Email inbox | Firm | AgentMail / domain mailbox | Maybe (DNS, billing) |
| Domain | Firm | Registrar API or human login | Often |
| GitHub org / pages | Firm | Token or human login | Often |
| Exchange account | Firm | KYC | Yes, once |
| Stripe / bank | Firm | KYC | Yes, once |
| VPS / GPU | Firm | Card or crypto VPS | Card = human once; crypto VPS = autonomous |
| Claude cognition | Firm | **Claude Pro/Max subscription via Claude Code CLI** | Yes, `claude login` once |

Unit of account: **USD** for reporting, **USDC** for operations.

Wallet policy:

- Hot wallet: operating float (default cap $1,500).
- Cold instruction: Treasurer writes a sweep playbook; keys never logged.
- Trading book: separate ledger sub-account, hard-capped.
- Operating cash is not tradable. Risk officer enforces the wall.

Bank account: Courier prepares the full KYC packet (entity name, activity
description, expected volumes, wallet addresses, Stripe/Mercury/Wise
checklist) and waits. Until then, the firm is crypto-native and can still get
paid in USDC.

---

## 3. Cognition: Claude subscription, not API

Live thinking goes through **Claude Code CLI**, which bills the Claude
Pro/Max subscription. The Anthropic HTTP API is a fallback only, off by
default, because it burns extra cash and defeats the point.

```
sovereign doctor          # sees whether `claude` is logged in
claude login              # human, once
sovereign run --mode live # workers call: claude -p --output-format json
```

### Model routing (token efficiency)

Most ticks **do not call a model**. Signals, bookkeeping, scraping, risk,
backtests, and settlement are code. Models are for language and novel
judgment.

| Tier | Alias | Use | Cadence |
| --- | --- | --- | --- |
| none | deterministic code | prices, risk, ledger, scrape, match jobs, size trades | every tick |
| `fast` | Haiku | classify, extract JSON, audit samples, human-message drafts | many/day |
| `work` | Sonnet | proposals, deliverables, playbook edits, product copy | per job |
| `think` | Opus | weekly council, strategy fights, new-play underwriting | 1–2×/week |

Hard rules:

1. Never Opus in the heartbeat.
2. Never send transcripts. Send a **state snapshot** (balances, missions,
   last 20 events, relevant playbook).
3. Cache a stable system preamble (role + policy). Only the snapshot changes.
4. Daily token budget (`model_budget_daily_tokens`). Surplus work queues.
5. Draft with Haiku, upgrade to Sonnet only if the Closer/Auditor score is
   below threshold.
6. One Sonnet delivery session per job, in a jailed work directory.
7. If subscription rate-limits, degrade to templates + queue. Do not fall
   through to paid API unless `allow_api_fallback: true`.

Expected load to hold $5k/mo labor (order of magnitude):

- ~15 proposals/day × ~1.5k tokens Sonnet
- ~4 deliveries/day × ~8–20k tokens Sonnet
- ~30 Haiku classifications
- weekly Opus council ~10–20k tokens

That is compatible with Max; on Pro, cut proposals and serialize deliveries.

---

## 4. Organization (agents that control each other)

```
                    ┌────────── Director ──────────┐
                    │  OKRs, play funding, ties     │
                    └─────────────┬────────────────┘
          ┌───────────────┬───────┴────────┬───────────────┐
     Treasurer          Risk           Auditor          Improver
     capital wall     freeze/thaw     sample & slash    evolve playbooks
          │               │                │                │
     ┌────┴────┬──────────┴───┬────────────┴─────┬──────────┴─────┐
  Hunter    Closer        Crafter           Trader         Publisher
  Scout     Operator      Courier           Bookkeeper
```

| Agent | Job | Model | Who can freeze / slash them |
| --- | --- | --- | --- |
| Director | Allocate attention and capital to plays that move $2k/$5k/$7k | think (weekly), else none | Auditor+Risk+Treasurer supermajority |
| Treasurer | Budgets, invoices, wallet sweeps, allocation | none / fast | Risk + Director |
| Risk | Circuit breakers, exposure, strategy certification gate | none | Director+Auditor (rare) |
| Auditor | Reviews trades, deliveries, outbound; reputation | fast | Director |
| Improver | A/B playbooks, promote winners | work | Auditor |
| Hunter | Find paid work | fast | Treasurer (budget), Auditor |
| Closer | Price, propose, negotiate | work | Auditor (quality), Ethics |
| Crafter | Do the work in a jail | work / think | Auditor (quality), Risk |
| Trader | Run only certified strategies | none | Risk (always) |
| Publisher | Package and list products | work | Auditor |
| Scout | New plays, markets, pricing | fast / think | Director |
| Operator | VPS, domains, tools purchases | none / fast | Treasurer + Risk |
| Courier | Human login queue, email | fast | Auditor |
| Bookkeeper | Recognize revenue, reports | none | Auditor |

No human approval. High-impact actions need **agent quorum**:

| Action | Quorum |
| --- | --- |
| Spend > agent autonomy (reputation-scaled) | Treasurer + Risk |
| Live trade | Certified strategy + Risk not frozen + within loss caps |
| Buy infra | Treasurer + Director |
| Outbound email / apply | Courier policy (no spam, no impersonation fraud) |
| Promote playbook | Improver + Auditor |
| Unfreeze an agent | Risk + Director |
| Remove Director | Treasurer + Risk + Auditor |

Reputation (0–100) scales autonomy. Repeated Auditor slashes starve a bad
agent of budget. Improver can replace its playbook. That is self-control.

---

## 5. Revenue plays (multiple options, all wired)

The engine runs a **portfolio of plays**. Director funds. Auditor kills.
Improver mutates. This is how $2k becomes $7k without a new idea from a human.

### Option A — Autonomous labor studio (primary, fastest to $2,000)

**What:** Find remote work the Crafter can actually ship: scripts, automations,
research briefs, landing copy, data cleaning, bot setups, code review,
technical writing.

**Why it hits $2k:** Four closed jobs at $500, or two at $1,000, in 30 days.
Agents work every tick. Platforms are a channel, not the identity: public
boards (RemoteOK, Arbeitnow, HN threads, direct outbound) plus email.

**Unit economics:**

- Close rate target: 8–15% of tailored proposals
- Delivery margin: ~90% (cognition is subscription-sunk)
- Cycle time: 24–72h for small jobs

**Agents:** Hunter, Closer, Crafter, Bookkeeper, Auditor.

**Risks:** Platform bans, unpaid clients, scope creep. Mitigations: crypto /
prepay preference, written scope, Auditor quality gate before send.

### Option B — Productized service (path to $5,000)

**What:** Turn the top repeating job into a named offer with a fixed price
and SLA. Examples the Scout should underwrite first:

1. "48h operations automation" (scripts + runbook) — $1,200
2. "Agent inbox + SOP" for a small business — $1,500 setup + $400/mo
3. "Weekly research memo" retainer — $800/mo
4. "Landing page + offer critique" — $600

**Why it hits $5k:** One $1.5k setup + two $800 retainers + overflow gigs.

**Agents:** Scout (positioning), Publisher (page), Closer (outbound), Crafter
(fulfill), Treasurer (recurring invoices).

### Option C — Digital products (asymmetric, slower)

**What:** Package internal tools: prompt packs with runnable playbooks,
niche scrapers, Notion/Airtable templates, tiny paid APIs, educational
backtests. Sell via Stripe/Gumroad (human login once) or USDC checkout.

**Why it belongs:** Zero marginal delivery. Compounds. Does not pay rent in
month one. Director keeps this ≤15% of attention until Option A is at
minimum target.

**Agents:** Crafter, Publisher, Scout.

### Option D — Systematic crypto trading (compounding, not rent)

**What:** Only strategies that pass `sovereign backtest` certification:

- Time-series momentum + volatility targeting (primary)
- Dual moving-average regime filter (capital preservation in bears)
- Mean-reversion is implemented but **must pass** the same gate; it often
  fails on crypto trends and that is a feature

**Proof bar (walk-forward):** Sharpe ≥ 0.5 on OOS, max drawdown ≤ 35%,
n_trades ≥ 10, costs 10 bps round-trip, published artifact on disk.

**Capital rule:** Trading book = Treasurer allocation, default 0 until
labor has produced a buffer. Max 1% of trading book risk per signal.
Daily halt −3%. Weekly halt −7%. Operating cash walled off.

**Why it will not print $2k/mo from $500:** 10% a month on $500 is $50.
Anyone promising otherwise is lying. Once the book is $20k–$50k, this
play matters. Until then it is paper + tiny size, proving the loop.

**Agents:** Trader, Risk, Auditor, Treasurer.

### Option E — B2B outbound for small businesses (high ceiling)

**What:** Courier + Closer sell done-for-you automations to local/online
SMBs (abandoned-cart texts, inventory alerts, report bots). Pay in USDC
or Stripe.

**Why it hits $7k:** A handful of $400–$800/mo retainers. Requires email
domain reputation and a human login for the first mailbox/DNS.

**Agents:** Scout, Closer, Crafter, Courier, Operator.

### Option F — Infra and arbitrage (opportunistic)

**What:** Operator buys cheap VPS with crypto, hosts products, resells
capacity, grabs expiring domains, runs affiliate offers the Ethics agent
allows (no fake scarcity, no malware).

**Agents:** Operator, Scout, Publisher.

### Recommended portfolio (what the engine actually starts)

Director’s default allocation of **attention** (not just dollars):

| Play | Until $2k | $2k–$5k | $5k–$7k+ |
| --- | --- | --- | --- |
| A Labor | 70% | 45% | 25% |
| B Productized / retainers | 15% | 30% | 35% |
| C Products | 5% | 10% | 15% |
| D Trading | 5% (paper/certify) | 10% | 15% |
| E/F experiments | 5% | 5% | 10% |

This mix is encoded in `sovereign/plays.py` and is mutated by Improver
when measured $/hour changes.

---

## 6. How $2k / $5k / $7k is actually hit

**$2,000 (weeks, not myths):**

- Hunter pulls public jobs every tick.
- Closer sends a small number of *specific* proposals (not spray).
- Crafter ships same day / next day.
- Bookkeeper recognizes cash on receipt (USDC or marked paid).
- Four modest wins or one retainer deposit.

**$5,000:**

- Convert one client to monthly.
- Productize the winning job type (Publisher).
- Keep a second gig channel so a single platform ban is not death.
- Trading still small.

**$7,000:**

- Two retainers or one retainer + a listed product with trickle sales.
- Labor becomes overflow, not the whole firm.
- Trading book only then gets more than dust.

Kill criteria (Auditor): play with 14 days of spend/attention and $0
expected value → defund. Scout must replace it.

---

## 7. Engine loop (no human in it)

Every tick:

1. Bookkeeper snapshots balances and 30-day run-rate vs $2k/$5k/$7k.
2. Risk applies freezes (drawdown, error rate, ethics flags).
3. Director assigns/renews missions from funded plays.
4. Workforce acts in parallel: Hunter, Trader, Publisher, Scout, Operator.
5. Closer on leads; Crafter on accepted jobs.
6. Treasurer settles invoices, refuses over-budget spends.
7. Auditor samples; slashes or boosts reputation.
8. Improver, if enough outcomes, writes a playbook trial.
9. Courier emits human login requests if a play is blocked on credentials.
   **Other plays continue.** Nothing global-halts for a login except Claude
   itself in live cognition mode (sim brain still runs).
10. Persist. Sleep. Repeat.

Self-improvement: every delivery and trade writes an Outcome. Improver
clusters failures ("proposals too generic", "missed scope") and patches
the playbook. Auditor A/B tests. Winners promote. That is the firm getting
smarter without you.

---

## 8. Bootstrap sequence (what happens on a real machine)

1. `pip install -e .` and `sovereign init`
2. Engine generates identity, ETH+SOL wallets, ledger, mandate.
3. Human: `claude login` (subscription). Courier will ask if missing.
4. `sovereign run --mode sim` until you trust the loop (paper jobs + paper
   trades + real backtests).
5. Human logins as requested, independently, whenever convenient:
   - Email/domain
   - Exchange (for live trading later)
   - Stripe or merchant (to collect fiat)
   - GitHub / VPS
6. `sovereign run --mode live` — labor can start as soon as Claude is
   logged in, even before Stripe. Collect in USDC.
7. First surplus: Treasurer splits buffer vs trading-book vs infra.

The firm does not wait for a bank to exist.

---

## 9. Legal and ethics (non-negotiable, encoded)

Agents may not:

- Phish, spoof, or socially engineer logins for third parties
- Fake testimonials, fake volume, pump tokens they hold
- Evade KYC or operate stolen identities
- Spam unsolicited bulk email
- Take unrestricted live market risk
- Exfiltrate wallet keys

They may:

- Act in the operator’s commercial name under the mandate
- Price, negotiate, deliver, invoice
- Trade certified strategies inside caps
- Buy compute and tools from the treasury
- Message the human for logins
- Rewrite their own playbooks subject to Auditor

This is an experiment and a real engine. It is not a promise of profit.
Markets and clients refuse sometimes. The loop keeps going.

---

## 10. What "done" looks like for this repository

- A plan with multiple plays and an org chart (this file).
- A running engine: init → tick → money movement in sim.
- Proven strategy pipeline: backtest → certify or reject.
- Mutual control: votes, freezes, reputation.
- Claude Code provider + mock/sim brain.
- Read-only dashboard; human inbox for logins only.
- Tests that exercise a full cycle.

After that, the constraint is credentials and time on the clock, not
missing software.
