# Model routing

Cognition provider order:

1. **Claude Code CLI** (`claude -p`) — Claude Pro/Max subscription
2. Deterministic/sim brain — always available
3. Anthropic API — **disabled** unless `allow_api_fallback: true`

## Tiers

| Config alias | Claude Code `--model` | When |
| --- | --- | --- |
| fast | haiku | classify, extract, cheap audit |
| work | sonnet | proposals, code, copy |
| think | opus | weekly council, underwriting |

## Budget

`model_budget_daily_tokens` is a soft cap. The router:

- counts estimated prompt+completion
- refuses think-tier if the day is > 70% spent
- downgrades work → fast for classification-like tasks
- never downgrades an in-flight Crafter delivery; queues the next one

## Prompt shape

```
[stable system: role + policy + output schema]
[playbook excerpt, max 1k tokens]
[snapshot: balances, missions, last events]
[task]
```

No chat history. No dumps of the database.
