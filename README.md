# agentic-money

An agentic personal-finance assistant. Add transactions in plain English and the
built-in agent categorizes them, tracks them against your budgets, and surfaces
actionable insights — all through a small dashboard backed by a TypeScript API.

## Stack

- **Runtime:** Node.js 20+ / TypeScript (ESM)
- **Server:** Express
- **Frontend:** static HTML/CSS/JS dashboard served by the API
- **Tests:** Vitest + Supertest
- **Lint/Types:** ESLint (flat config) + `tsc`

## Getting started

```bash
npm ci          # install dependencies (use `npm install` to refresh the lockfile)
npm run dev     # start the dev server with hot reload on http://localhost:3000
```

Then open http://localhost:3000. The server seeds a small demo dataset on first
run so the dashboard is populated.

## Scripts

| Command | Description |
| --- | --- |
| `npm run dev` | Start the API + dashboard with hot reload (`tsx watch`). |
| `npm run build` | Type-check and compile TypeScript to `dist/`. |
| `npm start` | Run the compiled server from `dist/`. |
| `npm run typecheck` | Type-check without emitting output. |
| `npm run lint` | Lint the codebase with ESLint. |
| `npm test` | Run the Vitest unit + API test suite. |

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness check. |
| `GET` | `/api/transactions` | List transactions (newest first). |
| `POST` | `/api/transactions` | Add a transaction; the agent infers the category. |
| `GET` | `/api/budgets` | List budgets. |
| `POST` | `/api/budgets` | Create/update a budget for a category. |
| `GET` | `/api/summary` | Totals, per-category breakdown, and agent insights. |
| `POST` | `/api/reset` | Clear all data. |

### Example

```bash
curl -X POST http://localhost:3000/api/transactions \
  -H 'Content-Type: application/json' \
  -d '{"description":"Whole Foods groceries","amount":-82.15}'
```

The response includes the agent-assigned `category` (`groceries` in this case).

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | `3000` | Port the server listens on. |
| `HOST` | `0.0.0.0` | Interface to bind. |
| `DATA_FILE` | `data/store.json` | JSON file used to persist state. |

## Development environment

This repository is configured for [Cursor Cloud Agents](https://cursor.com/docs/cloud-agent/setup)
via `.cursor/environment.json`: dependencies install with `npm ci`, and the dev
server runs in a persistent terminal on port 3000.
