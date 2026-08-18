# Deploy

Package, probe, and supervise one Sovereign data directory. The engine is
still one process; this is how you run that process as a service.

## Container

From the repository root:

```bash
docker build -t sovereign .
docker run --rm -v sovereign-data:/data sovereign init --data-dir /data --mode sim
docker run --rm -v sovereign-data:/data sovereign healthcheck --data-dir /data
docker run -d --name sovereign -v sovereign-data:/data sovereign serve --data-dir /data --mode sim
```

The image user is uid 10001. `/data` is the only writable volume. Secrets
and `master.key` must be created at runtime — they are not in the image
(`.dockerignore` excludes `*.enc` and `master.key`).

The image `HEALTHCHECK` runs `sovereign healthcheck` (readiness). Compose
adds `--stale-seconds 300` for liveness.

### Compose

```bash
export SOVEREIGN_DASHBOARD_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose -f deploy/docker-compose.yml up --build
```

- `engine` runs `sovereign serve`
- `dashboard` publishes `127.0.0.1:7474` and **requires** the bearer token
  (it binds `0.0.0.0` inside the container so published ports work)
- Set `SOVEREIGN_MODE=live` only after `claude` is available in the image
  or you have configured `models.provider: api` with a vaulted key

Live mode in this slim image has no Claude Code CLI. Either:

- stay on `sim`, or
- set `models.provider: api` and vault `ANTHROPIC_API_KEY`, or
- extend the image with the CLI and a login volume

## systemd

`deploy/sovereign.service` is a template:

1. `pip install .` (or the wheel) so `/usr/local/bin/sovereign` exists
2. `useradd --system --home /var/lib/sovereign sovereign`
3. Copy the unit to `/etc/systemd/system/sovereign.service`
4. `sovereign init --data-dir /var/lib/sovereign --mode live`
5. `systemctl enable --now sovereign`

The unit uses `ProtectSystem=strict` and `ReadWritePaths=/var/lib/sovereign`.
A liveness timer can call:

```bash
sovereign healthcheck --data-dir /var/lib/sovereign --stale-seconds 300
```

## Probes

| Probe | Command | Passes when |
| --- | --- | --- |
| Readiness | `sovereign healthcheck --data-dir DIR` | Required readiness checks are ok |
| Liveness | `sovereign healthcheck --data-dir DIR --stale-seconds N` | Ready **and** last tick younger than N seconds |

A missing tick timestamp fails only the liveness form (fresh init before
the first tick). `start_period` / `TimeoutStartSec` should cover that first
tick.

## Host hardening

- Mode `600` on `secrets.enc` and `master.key`; prefer the keyring backend
  so the key is not next to the ciphertext
- Dashboard: loopback or bearer token + TLS
- Backups: `sovereign backup --out` on a path **outside** the data dir;
  store the master key separately; rehearse with `--restore-drill`
- Do not run two units against the same directory
