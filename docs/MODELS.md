# Model routing

Current routing is mode-dependent:

1. Sim mode uses the deterministic brain.
2. Live mode uses **Claude Code CLI** (`claude -p`) when the configured
   provider is `claude_code` and its executable is available.
3. If the provider or executable is unavailable, a Claude invocation fails,
   or the router is already degraded, live completions return an empty result
   and record degraded/queued state. They do not substitute simulated content.

There is no Anthropic HTTP provider implementation. Despite its legacy name,
`allow_api_fallback` does not enable one. When set, the recorded error makes
the missing API fallback explicit, but ordinary and jailed completion behavior
remains fail-closed.

## Tiers

| Config alias | Claude Code `--model` | When |
| --- | --- | --- |
| fast | haiku | classify, extract, cheap audit |
| work | sonnet | proposals, code, copy |
| think | opus | weekly council, underwriting |

## Budget

`daily_token_budget` defaults to 400,000. The router stores an approximate
character-based token count, not Claude subscription usage, and resets it on
the first call of each UTC day. For ordinary `complete` calls it:

- downgrades `think` to `work` after recorded usage exceeds 70% of the budget
- estimates the next prompt before dispatch
- returns an empty result and increments degraded/queued state in live mode
  when that estimate exceeds the remainder
- uses the deterministic brain in sim mode when the estimate exceeds the
  remainder, then records that generated text

Live `complete_in_dir` work enforces the same remaining-budget gate; sim
`complete_in_dir` work always uses the deterministic brain and records the
generated text. There is no automatic `work` → `fast` classification
downgrade. Degraded state clears on the first call of a new UTC day.

## Market cadence is separate

Model degraded/queued state does not control market scheduling. In live mode,
price refresh defaults to every hour. Certification also retries hourly while
no certification report list exists, then uses the normal weekly cadence after
any report list is stored. The latter condition is report existence—not a
passing strategy—so all-rejected and insufficient-data reports use the weekly
cadence too. Configure these independently with
`live_timing.price_refresh_hours`,
`live_timing.certification_retry_hours`, and `live_timing.recertify_hours`.

## Prompt shape

```
[stable system: role + policy + output schema]
[playbook excerpt, max 1k tokens]
[snapshot: balances, missions, last events]
[task]
```

No chat history. No dumps of the database.
