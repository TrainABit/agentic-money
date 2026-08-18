# Bootstrap

The engine does not wait for a bank.

1. `pip install -e ".[dev]"`
2. `sovereign bootstrap` — **recommended**: one shot that creates the
   identity and mnemonic-derived ETH/Solana wallets (encrypted in
   `data/secrets.enc`), runs the full idempotent repair, and prints a
   readiness report. Exit code 0 means every required check passed: Python
   ≥ 3.11, engine health findings, spec/registry tool alignment, and a
   message-bus roundtrip (plus the Claude CLI in live mode). Informational
   checks (legacy wallet backup, comms backlog, certification count) are
   printed but never block.
   Manual alternative, same paths as separate commands: `sovereign init`
   (identity + wallets), then `sovereign setup` (repair engine files), then
   `sovereign doctor --fix` (heal again + CLI/wallet checks).
3. `claude login` — Claude Pro/Max subscription (not an API key). Sim brain already runs without this.
4. `sovereign run --mode sim --ticks 30` — prove the loop
5. `sovereign dashboard` — observe only (health + tools included); the default
   loopback bind needs no token
6. Fill `sovereign inbox` items when you can (`sovereign reply hr_0001 ok=1`, or `SMTP_PASS=-` and type the secret on stdin). Replies go into the encrypted vault; values are not kept in the inbox JSON.
7. Optional real email: `pip install -e ".[mail]"`, then reply to the courier's
   `agentmail` request with `AGENTMAIL_API_KEY` and `AGENTMAIL_INBOX_ID`.
   Outbound proposals/invoices then send through AgentMail and the courier
   polls the inbox every `live_timing.mail_poll_minutes` (default 5), feeding
   replies into the same authorized drop-in pipeline. SMTP credentials remain
   the fallback; with neither, the file outbox still works.
8. `sovereign serve --mode live` once Claude is logged in. The daemon runs the
   readiness gate first and refuses an unready live start (`--force`
   overrides after you have read the failing checks).
9. Schedule `sovereign backup --out /path/outside/the/box` (and test restores
   with `sovereign backup --verify`). Rehearse without touching live data
   with `sovereign backup --restore-drill /tmp/drill`. The backup contains
   the online SQLite snapshot, playbooks, invoices, artifacts, and
   `secrets.enc` — never `data/master.key`, which you must store separately
   for the backup to be decryptable.
10. Optional OS-keyring custody: `pip install -e ".[keyring]"`, then set
    `wallet.master_key_backend: keyring` in `data/config.yaml` before the
    first init (or `sovereign rotate-key --confirm --to-keyring` later).
11. Containers and systemd: [`docs/DEPLOY.md`](DEPLOY.md). Day-two
    operations: [`docs/RUNBOOK.md`](RUNBOOK.md).

If something is broken (corrupt `human_inbox.json`, missing playbook, stale
`engine.lock`, unbound tools), do not start from scratch: `sovereign setup`
is idempotent and the Mechanic agent runs the same repairs every tick.
Re-running `sovereign bootstrap` is equally safe — it repeats the same
repair and tells you whether the engine is ready afterwards.

Live jobs stay `applied` until `sovereign accept JOB_ID` or a JSON drop in `data/mail/inbox/` whose subject contains the job id and `ACCEPTED`. Listings with no contact email are marked `needs_channel` (drop a lead JSON or apply at the URL). Invoices stay open until USDC hits the ETH or Solana address or `sovereign paid INV_OR_JOB`. Email text never marks an invoice paid.

Live prices refresh hourly by default. If no certification report list exists,
the engine retries certification hourly; after any report list exists, it uses
the normal weekly recertification cadence. This is not a retry-until-pass
loop—rejected and insufficient-data reports also select the weekly cadence.
The intervals are configurable under `live_timing`.

`data/master.key` decrypts `data/secrets.enc`. Keep the key off backups of the
ciphertext if you can; mode `600` is the local floor, not a vault. Existing
encrypted wallet bundles are intentionally loaded without automatic
migration. Legacy random Solana wallets cannot be restored from the mnemonic:
until explicitly migrated, they remain recoverable only from the encrypted
`sol_secret` in `data/secrets.enc` together with `data/master.key`. Preserve
both files.

To expose the dashboard beyond loopback, set a strong bearer token first. The
server refuses a non-loopback bind without it, and all `/api/*` requests then
require the token. After the first unauthorized API response, the browser UI
shows a password input and sends the value in the `Authorization` header. It
stores the value only in session storage and never places it in the URL.

```bash
export SOVEREIGN_DASHBOARD_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
sovereign dashboard --host 0.0.0.0
```

Use TLS or a TLS-terminating reverse proxy for remote access.

Optional later (Courier will ask; other plays keep running):

- Exchange API (spot, withdraw disabled) for live trading
- Stripe or merchant for fiat
- Domain DNS for email
- VPS API if you want the Operator to buy a box
- Upwork/Fiverr tokens if you want those boards (public boards already work)

Keys never go in git. `SOVEREIGN_CONFIRM_REVEAL=1 sovereign wallet --reveal` is the only mnemonic dump.
