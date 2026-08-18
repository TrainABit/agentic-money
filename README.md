# agentic-money

An agentic personal-finance assistant. Add transactions in plain English and the
built-in agent categorizes them, tracks them against your budgets, and surfaces
actionable insights — all through a small dashboard backed by a TypeScript API.

## Stack

- **Runtime:** Node.js 22.13+ / TypeScript (ESM); `.nvmrc` pins 22.14.0
- **Server:** Express 5
- **Storage:** SQLite (WAL mode, integer minor units)
- **Frontend:** static HTML/CSS/JS dashboard served by the API
- **Tests:** Vitest + Supertest
- **Lint/Types:** ESLint (flat config) + `tsc`

## Getting started

```bash
nvm use         # optional; selects the pinned Node 22.14.0
npm ci          # install dependencies (use `npm install` to refresh the lockfile)
npm run dev     # start the dev server with hot reload on http://localhost:3000
```

Then open http://localhost:3000. The database starts empty. To add the demo
records explicitly, start once with `SEED_DEMO=true npm run dev`; the seed is
transactional and idempotent.

Data is stored in `data/agentic-money.sqlite` by default. The path is anchored
to the project directory, so development and compiled production use the same
file. See [Legacy JSON migration](#legacy-json-migration) for the one-time
`data/store.json` import contract.

`better-sqlite3` is a native dependency. Supported Node/platform combinations
normally install a prebuilt binary. If a prebuild is unavailable, installation
requires Python 3, `make`, a C/C++ compiler, and the platform prerequisites for
`node-gyp`. Use Node 22.13 or newer; CI and `.nvmrc` use Node 22.14.0.

## Scripts

| Command | Description |
| --- | --- |
| `npm run dev` | Start the API + dashboard with hot reload (`tsx watch`). |
| `npm run build` | Type-check and compile TypeScript to `dist/`. |
| `npm start` | Run the compiled server from `dist/`. |
| `npm run typecheck` | Type-check without emitting output. |
| `npm run lint` | Lint server, tests, scripts, and browser JavaScript. |
| `npm test` | Run the Vitest unit + API test suite. |
| `npm run smoke` | Smoke-test the compiled server on an ephemeral port. |

## Production

Build and start from the repository root:

```bash
npm ci
npm run build
npm start
```

The compiler emits the entry point at `dist/server.js`. A deployment must keep
`public/`, `dist/`, `package.json`, `package-lock.json`, and production
`node_modules` together at the repository root. Persist the `data/` directory,
or set `DATA_FILE` to a path on a persistent volume.

The default bind is `127.0.0.1`. Binding to a public or container interface such
as `0.0.0.0` is refused unless `API_TOKEN` is set:

```bash
HOST=0.0.0.0 API_TOKEN='replace-with-a-secret' npm start
```

When a token is configured, every mutating endpoint accepts either
`Authorization: Bearer <token>` or `X-API-Token: <token>`. Read-only endpoints
remain available for the dashboard. The optional reset endpoint is absent
unless `ENABLE_RESET=true`, and enabling it also requires `API_TOKEN`. The
dashboard keeps a supplied token only in page memory and clears it on reload.

When deploying behind a reverse proxy, set `TRUST_PROXY` to the exact number of
trusted proxy hops (commonly `1`). It defaults to `0`, so forwarded client
addresses are ignored. This setting is applied before API rate limiting; do not
set it higher than the number of proxies you control.

The process handles `SIGINT` and `SIGTERM`, stops accepting new connections,
allows active requests to finish, closes SQLite, and then exits.

This dashboard never places orders and never accepts a Hyperliquid private
key. Optional read-only market data:

```bash
HYPERLIQUID_INFO_URL=https://api.hyperliquid-testnet.xyz/info
HYPERLIQUID_COINS=BTC,ETH
HYPERLIQUID_ADDRESS=0xYourPublicAddress   # optional clearinghouse lookup
```

Setting `HYPERLIQUID_PRIVATE_KEY` (or similar) is refused at startup. Live
execution lives in Sovereign on Hyperliquid (`docs/TRADING.md` in that tree).

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness and bounded SQLite `PRAGMA quick_check(1)` result. |
| `GET` | `/api/transactions?limit=50&offset=0` | Paginated transactions, newest first (`limit` max 100). |
| `POST` | `/api/transactions` | Add a transaction; the agent infers the category. |
| `GET` | `/api/budgets` | List budgets. |
| `POST` | `/api/budgets` | Create/update a budget for a category. |
| `GET` | `/api/summary` | Totals, per-category breakdown, and agent insights. |
| `GET` | `/api/hyperliquid` | Read-only Hyperliquid mids (optional account if `HYPERLIQUID_ADDRESS` is set). |
| `POST` | `/api/reset` | Clear transactions and budgets (disabled by default). |

### Example

```bash
curl -X POST http://localhost:3000/api/transactions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer replace-with-a-secret' \
  -d '{"description":"Whole Foods groceries","amount":-82.15}'
```

The response includes the agent-assigned `category` (`groceries` in this case).
Amounts and budget limits are rounded to cents before storage. Positive
transactions must be income, negative transactions cannot be income, and
budgets apply only to spending categories. Errors consistently use:

```json
{
  "error": {
    "code": "validation_error",
    "message": "amount must be a finite number"
  }
}
```

`GET /api/transactions` returns an envelope rather than a bare array:

```json
{
  "data": [],
  "pagination": {
    "limit": 50,
    "offset": 0,
    "total": 0,
    "hasMore": false
  }
}
```

`limit` must be between 1 and 100, and `offset` must be a non-negative integer.
The dashboard exposes Previous/Next controls and a visible range indicator; it
shows 20 records per page and uses `hasMore` to determine whether another page
is available.

`GET /api/health` returns HTTP 200 only when the bounded SQLite check reports
`ok`; a closed, unavailable, or corrupt store returns HTTP 503:

```json
{
  "status": "ok",
  "storage": {
    "status": "ok",
    "type": "sqlite",
    "persistent": true
  }
}
```

## Legacy JSON migration

When the default SQLite database is first created, the server imports
`data/store.json` if it exists. `LEGACY_DATA_FILE` can specify a different
source. The migration is transactional and marked complete in SQLite so it is
not repeated:

- Amounts and limits are normalized to integer cents.
- A positive transaction with a spending category becomes `income`.
- A negative transaction categorized as `income` is recategorized from its
  description, falling back to `other`.
- Legacy `income` budgets are validated and then omitted.
- Unknown categories, duplicate IDs/categories, invalid dates, zero/non-finite
  amounts, negative/non-finite limits, malformed JSON, and other corrupt input
  stop startup without a partial import.
- The source JSON is always preserved. Passing a legacy JSON path directly to
  `Store` creates a companion `<path>.sqlite` database.

An existing zero-byte `DATA_FILE` is treated as corrupt, not as a new SQLite
database. Remove or replace it explicitly after recovering any required data.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `3000` | Port the server listens on. |
| `HOST` | `127.0.0.1` | Interface to bind; non-loopback requires `API_TOKEN`. |
| `DATA_FILE` | `data/agentic-money.sqlite` | SQLite database path, relative to the project root unless absolute. |
| `LEGACY_DATA_FILE` | `data/store.json` on the default data path | Optional legacy JSON file to import once. |
| `API_TOKEN` | unset | Token required for mutations when configured and for every non-loopback bind. |
| `ENABLE_RESET` | `false` | Expose `POST /api/reset`; requires `API_TOKEN`. |
| `SEED_DEMO` | `false` | Run the idempotent demo seed transaction. |
| `API_RATE_LIMIT` | `120` | Maximum API requests per client per minute. |
| `TRUST_PROXY` | `0` | Number of trusted reverse-proxy hops used to derive client IPs. |

## Development environment

This repository is configured for [Cursor Cloud Agents](https://cursor.com/docs/cloud-agent/setup)
via `.cursor/environment.json`: dependencies install with `npm ci`, and the dev
server runs in a persistent terminal on port 3000.
