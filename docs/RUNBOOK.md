# Operator runbook

Day-two operations for a running Sovereign data directory. Pair with
[`BOOTSTRAP.md`](BOOTSTRAP.md) (first boot) and [`DEPLOY.md`](DEPLOY.md)
(containers / systemd).

## Daily

```bash
sovereign healthcheck --data-dir data                  # readiness
sovereign healthcheck --data-dir data --stale-seconds 300
sovereign status --data-dir data
sovereign comms --status dead
sovereign alerts
```

`healthcheck` never raises: exit 0 means ready (and not stale when a bound
is set). Use it as a Docker `HEALTHCHECK` or a systemd timer.

## Weekly

```bash
sovereign backup --out /mnt/backups/sovereign-$(date +%F)
sovereign backup --verify /mnt/backups/sovereign-$(date +%F)
sovereign backup --restore-drill /tmp/sovereign-drill-$(date +%F)
sovereign maintain --data-dir data
```

`restore-drill` writes a fresh backup under `DIR/backup`, verifies hashes
and `PRAGMA quick_check`, and probes schema version / row counts. It never
writes back onto the live data dir. Keep `master.key` (or the keyring
entry) **off** the backup disk.

After `maintain`, event rows sit at `retention.event_rows` (default
10_000) and done/expired comms older than `retention.comms_days` (default
30) are gone. VACUUM is on unless `--no-vacuum`.

## Schema

```bash
sovereign migrate --data-dir data
```

Opening the engine applies pending migrations. `migrate` is the explicit
operator command: it prints `schema_version`, `current`, and `schema_log`.
A database from before versioning (`user_version = 0`) lands on the
current version without data loss.

## Secrets

Default custody is `data/master.key` next to `data/secrets.enc`. Encryption
at rest only helps if the key is copied separately.

Opt into the OS keyring (`pip install -e ".[keyring]"`):

```yaml
# data/config.yaml
wallet:
  master_key_backend: keyring
  keyring_service: sovereign
  keyring_username: master_key
```

Rotate the current backend (stop `sovereign serve` first, keep a backup):

```bash
sovereign rotate-key --data-dir data --confirm
```

Move file custody into the keyring and drop the old file:

```bash
sovereign rotate-key --data-dir data --confirm --to-keyring --delete-old-file
```

Mnemonic reveal is still `SOVEREIGN_CONFIRM_REVEAL=1 sovereign wallet --reveal`.

## Incidents

| Symptom | First check | Action |
| --- | --- | --- |
| Live daemon will not start | `sovereign bootstrap` readiness | Fix required checks or `--force` after reading them |
| Tick stuck / probe stale | `healthcheck --stale-seconds` / `sovereign debug --show` | Restart the unit; inspect `data/logs/trace` |
| Dead letters | `sovereign comms --status dead` | `--requeue MSG_ID` or fix the handler |
| Invariant / halt alerts | `sovereign alerts` | Do not `--force` past a broken ledger |
| Unattributed USDC | treasurer events + `chain_txids` | Manual `paid --confirm` only after you match the tx |
| Legacy SOL wallet | `wallet_backup` informational | Preserve `secrets.enc` + master key; do not rotate blindly |

`sovereign doctor --fix` and Mechanic run the same repairs (paths,
playbooks, inbox, stale lock, tools). Prefer that over deleting `data/`.

## Do not

- Copy `master.key` into a backup of `secrets.enc`
- Run two `serve`/`run` processes on one data dir (the lock is the gate)
- Treat email "paid" as settlement
- Bind the dashboard off loopback without `SOVEREIGN_DASHBOARD_TOKEN` and TLS
- Enable `web` / `mcp` with an empty mental model of the allowlists
