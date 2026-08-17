# Bootstrap

The engine does not wait for a bank.

1. `pip install -e ".[dev]"`
2. `sovereign init` — identity + ETH/SOL wallets (encrypted in `data/secrets.enc`)
3. `claude login` — Claude Pro/Max subscription (not an API key). Sim brain already runs without this.
4. `sovereign setup` then `sovereign doctor --fix` — repair engine files, then check Claude
5. `sovereign run --mode sim --ticks 30` — prove the loop
6. `sovereign dashboard` — observe only (health + tools included)
7. Fill `sovereign inbox` items when you can (`sovereign reply hr_0001 ok=1`). Replies go into the encrypted vault and unblock that play.
8. `sovereign serve --mode live` once Claude is logged in.

If something is broken (corrupt `human_inbox.json`, missing playbook, stale
`engine.lock`, unbound tools), do not start from scratch: `sovereign setup`
is idempotent and the Mechanic agent runs the same repairs every tick.

Live jobs stay `applied` until `sovereign accept JOB_ID` or a JSON drop in `data/mail/inbox/` whose subject contains the job id and `ACCEPTED`. Invoices stay open until USDC hits the ETH address or `sovereign paid INV_OR_JOB`.

Optional later (Courier will ask; other plays keep running):

- Exchange API (spot, withdraw disabled) for live trading
- Stripe or merchant for fiat
- Domain DNS for email
- VPS API if you want the Operator to buy a box
- Upwork/Fiverr tokens if you want those boards (public boards already work)

Keys never go in git. `SOVEREIGN_CONFIRM_REVEAL=1 sovereign wallet --reveal` is the only mnemonic dump.
