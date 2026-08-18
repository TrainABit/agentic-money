from __future__ import annotations

import ipaddress
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from sovereign import ops
from sovereign.config import EngineConfig
from sovereign.engine.world import World, bootstrap

T = TypeVar("T")

HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8"/>
<title>Sovereign</title>
<style>
  :root { color-scheme: dark; }
  body { font: 14px/1.45 ui-sans-serif, system-ui; margin: 0; background: #0b0d10; color: #e8edf2; }
  header { padding: 20px 28px; border-bottom: 1px solid #1e2630; display:flex; justify-content:space-between; align-items:center; }
  h1 { font-size: 18px; margin: 0; letter-spacing: .04em; }
  main { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 20px 28px 40px; }
  section { background: #12171d; border: 1px solid #1e2630; border-radius: 12px; padding: 16px 18px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: #8ea0b3; margin: 0 0 10px; }
  .bar { height: 8px; background: #1e2630; border-radius: 99px; overflow: hidden; margin: 6px 0 12px; }
  .bar > i { display:block; height:100%; background: linear-gradient(90deg, #3dd6c6, #7cff6b); }
  pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; white-space: pre-wrap; max-height: 260px; overflow: auto; color: #c5d0dc; margin: 0; }
  .pills { display:flex; flex-wrap:wrap; gap:8px; }
  .pill { background:#1e2630; border-radius:999px; padding:4px 10px; font-size:12px; }
  .pill.alert { background:#4a1d24; color:#ffb3bd; }
  #dead-letters { margin:0; padding-left:18px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:12px; color:#c5d0dc; max-height:160px; overflow:auto; }
  #observer-auth { display:flex; gap:8px; align-items:center; margin-top:8px; }
  #observer-auth[hidden] { display:none; }
  #observer-token { background:#0b0d10; border:1px solid #33404e; border-radius:6px; color:#e8edf2; padding:5px 8px; }
  #observer-connect { background:#1e2630; border:1px solid #445466; border-radius:6px; color:#e8edf2; padding:5px 9px; cursor:pointer; }
</style>
<header>
  <h1>SOVEREIGN <span id="firm" style="color:#8ea0b3;font-weight:500"></span></h1>
  <div>
    <div id="meta"></div>
    <div id="observer-auth" hidden>
      <label for="observer-token">Bearer token</label>
      <input id="observer-token" type="password" autocomplete="current-password"/>
      <button id="observer-connect" type="button">Connect</button>
    </div>
  </div>
</header>
<main>
  <section>
    <h2>Trailing 30d vs goals</h2>
    <div id="goals"></div>
    <h2>Pipeline</h2>
    <div class="pills" id="pills"></div>
  </section>
  <section>
    <h2>Treasury</h2>
    <pre id="treas"></pre>
  </section>
  <section>
    <h2>Wallets / credentials present</h2>
    <pre id="wallet"></pre>
    <h2>Human inbox</h2>
    <pre id="inbox"></pre>
  </section>
  <section>
    <h2>Invoices & offers</h2>
    <pre id="inv"></pre>
  </section>
  <section>
    <h2>Strategies</h2>
    <pre id="strat"></pre>
  </section>
  <section>
    <h2>Agents</h2>
    <pre id="agents"></pre>
  </section>
  <section>
    <h2>Health / tools</h2>
    <pre id="health"></pre>
  </section>
  <section>
    <h2>Runtime</h2>
    <div class="pills" id="runtime"></div>
    <h2>Recent agent errors</h2>
    <pre id="agent-errors"></pre>
    <h2>Dead letters</h2>
    <ul id="dead-letters"></ul>
  </section>
  <section style="grid-column: 1 / -1">
    <h2>Recent events</h2>
    <pre id="events"></pre>
  </section>
</main>
<script>
const TOKEN_KEY = 'sovereign_dashboard_token';
const byId = (id) => document.getElementById(id);

function showTokenInput() {
  byId('observer-auth').hidden = false;
  byId('observer-token').focus();
}

async function fetchJson(path) {
  const token = sessionStorage.getItem(TOKEN_KEY);
  const response = await fetch(path, {
    headers: token ? {'Authorization': 'Bearer ' + token} : {}
  });
  if (response.status === 401) {
    sessionStorage.removeItem(TOKEN_KEY);
    showTokenInput();
    throw new Error('Dashboard bearer token required');
  }
  if (!response.ok) throw new Error('Dashboard request failed: HTTP ' + response.status);
  return response.json();
}

function numberOrZero(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function makePill(text) {
  const pill = document.createElement('span');
  pill.className = 'pill';
  pill.textContent = text;
  return pill;
}

function renderGoals(goals) {
  const fragment = document.createDocumentFragment();
  const runRate = numberOrZero(goals.run_rate_usd);
  const lifetime = numberOrZero(goals.lifetime_usd);
  const rows = [
    ['Minimum $' + numberOrZero(goals.minimum), goals.progress_min],
    ['Recommended $' + numberOrZero(goals.recommended), goals.progress_rec],
    ['Good $' + numberOrZero(goals.good), goals.progress_good],
  ];
  for (const [labelText, rawProgress] of rows) {
    const label = document.createElement('div');
    label.textContent = labelText + ' — trailing $' + runRate.toFixed(0) +
      ' / life $' + lifetime.toFixed(0);
    const bar = document.createElement('div');
    bar.className = 'bar';
    const fill = document.createElement('i');
    const progress = Math.max(0, Math.min(1, numberOrZero(rawProgress)));
    fill.style.width = (progress * 100).toFixed(1) + '%';
    bar.appendChild(fill);
    fragment.append(label, bar);
  }
  byId('goals').replaceChildren(fragment);
}

function renderPipeline(status) {
  const fragment = document.createDocumentFragment();
  for (const [name, count] of Object.entries(status.pipeline || {})) {
    fragment.appendChild(makePill(String(name) + ' ' + String(count)));
  }
  fragment.appendChild(makePill('invoices open ' + String(status.invoices_open)));
  fragment.appendChild(makePill('mail out ' + String(status.mail_out)));
  byId('pills').replaceChildren(fragment);
}

function renderRuntime(runtime) {
  const ticks = runtime.ticks || {};
  const fragment = document.createDocumentFragment();
  fragment.appendChild(makePill('tick avg ' + numberOrZero(ticks.avg_ms).toFixed(1) + ' ms'));
  fragment.appendChild(makePill('tick last ' + numberOrZero(ticks.last_ms).toFixed(1) + ' ms'));
  for (const [name, count] of Object.entries(runtime.comms || {})) {
    const pill = makePill('comms ' + String(name) + ' ' + String(count));
    if (name === 'dead' && numberOrZero(count) > 0) pill.classList.add('alert');
    fragment.appendChild(pill);
  }
  byId('runtime').replaceChildren(fragment);
  const errors = (runtime.agents || {}).recent_errors || {};
  byId('agent-errors').textContent = Object.keys(errors).length
    ? JSON.stringify(errors, null, 2)
    : 'none';
}

function renderDeadLetters(rows) {
  const items = Array.isArray(rows) ? rows : [];
  const fragment = document.createDocumentFragment();
  if (!items.length) {
    const none = document.createElement('li');
    none.textContent = 'none';
    fragment.appendChild(none);
  }
  for (const row of items) {
    const item = document.createElement('li');
    item.textContent = String(row.ts) + ' · ' + String(row.kind) + ' ' +
      String(row.sender) + ' → ' + String(row.recipient) +
      ' · attempts ' + String(row.attempts) +
      (row.error ? ' · ' + String(row.error) : '');
    fragment.appendChild(item);
  }
  byId('dead-letters').replaceChildren(fragment);
}

async function tick() {
  try {
    const [s, runtime, deadLetters] = await Promise.all([
      fetchJson('/api/status'),
      fetchJson('/api/metrics'),
      fetchJson('/api/comms?status=dead&limit=8'),
    ]);
    const cognition = s.cognition || {};
    byId('firm').textContent = String(s.firm) + ' · ' + String(s.mode) + ' · tick ' + String(s.tick);
    byId('meta').textContent = String(cognition.provider) + ' · tokens ' + String(cognition.tokens);
    renderGoals(s.goals || {});
    renderPipeline(s);
    byId('treas').textContent = JSON.stringify(s.ledger, null, 2);
    byId('wallet').textContent = JSON.stringify({wallet:s.wallet, credentials:s.credentials_present}, null, 2);
    byId('inbox').textContent = JSON.stringify(s.human_inbox, null, 2);
    byId('inv').textContent = JSON.stringify({open:s.invoices_open, paid:s.invoices_paid, offers:s.offers}, null, 2);
    byId('strat').textContent = JSON.stringify({certified:s.certified_strategies, rejected:s.rejected_strategies}, null, 2);
    byId('agents').textContent = JSON.stringify({frozen:s.frozen_agents, reputation:s.reputation, broker:s.broker}, null, 2);
    const toolCount = (s.tools && s.tools.names) ? s.tools.names.length : 0;
    byId('health').textContent = JSON.stringify({health:s.health, tools: toolCount, skills:s.skills}, null, 2);
    byId('events').textContent = JSON.stringify((s.recent_events || []).slice(0,12), null, 2);
    renderRuntime(runtime);
    renderDeadLetters(deadLetters);
  } catch (error) {
    byId('meta').textContent = error instanceof Error ? error.message : 'Dashboard request failed';
  }
}

async function connectWithToken() {
  const input = byId('observer-token');
  const token = input.value.trim();
  if (!token) {
    byId('meta').textContent = 'Dashboard bearer token required';
    return;
  }
  sessionStorage.setItem(TOKEN_KEY, token);
  input.value = '';
  byId('observer-auth').hidden = true;
  await tick();
}

byId('observer-connect').addEventListener('click', connectWithToken);
byId('observer-token').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') connectWithToken();
});
tick();
// Poll every 5s, but never while the tab is hidden; catch up as soon as
// the tab becomes visible again.
setInterval(() => { if (!document.hidden) tick(); }, 5000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) tick();
});
</script>
</html>
"""


def create_app(data_dir: str, mode: str) -> FastAPI:
    app = FastAPI(title="Sovereign", docs_url=None, redoc_url=None)
    api = APIRouter(prefix="/api")
    cached: dict[str, World | None] = {"world": None}
    world_lock = RLock()
    expected_token = (os.environ.get("SOVEREIGN_DASHBOARD_TOKEN") or "").strip() or None

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if expected_token is None:
            return
        scheme, separator, credential = (authorization or "").partition(" ")
        valid = (
            bool(separator)
            and scheme.lower() == "bearer"
            and secrets.compare_digest(credential, expected_token)
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="valid dashboard bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    api.dependencies.append(Depends(authorize))

    def read_world(reader: Callable[[World], T]) -> T:
        """Serialize a full reload/read/render cycle on the cached connection."""
        with world_lock:
            w = cached["world"]
            if w is None:
                w = bootstrap(EngineConfig(mode=mode, data_dir=Path(data_dir)), heal=False)  # type: ignore[arg-type]
                cached["world"] = w
            else:
                w.load_kv()
            return reader(w)

    def world_response(reader: Callable[[World], Any]) -> JSONResponse:
        return read_world(lambda w: JSONResponse(reader(w)))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return HTML

    @api.get("/status")
    def status() -> JSONResponse:
        return world_response(lambda w: w.status())

    @api.get("/invoices")
    def invoices() -> JSONResponse:
        return world_response(lambda w: w.store.invoices())

    @api.get("/jobs")
    def jobs() -> JSONResponse:
        return world_response(lambda w: w.store.jobs())

    @api.get("/health")
    def health() -> JSONResponse:
        return world_response(lambda w: w.store.get_kv("health") or {})

    @api.get("/metrics")
    def metrics() -> JSONResponse:
        return world_response(ops.metrics)

    @api.get("/comms")
    def comms(status: str | None = None, limit: int = 50) -> JSONResponse:
        try:
            return world_response(
                lambda w: ops.sanitized_messages(w, status=status or None, limit=limit)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/tools")
    def tools() -> JSONResponse:
        def manifest(w: World) -> dict[str, Any]:
            if w.tools is None:
                return {"names": []}
            return w.tools.manifest()

        return world_response(manifest)

    app.include_router(api)
    return app


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def serve(data_dir: str, mode: str, host: str, port: int) -> None:
    token = (os.environ.get("SOVEREIGN_DASHBOARD_TOKEN") or "").strip()
    if not _is_loopback_host(host) and not token:
        raise RuntimeError(
            "refusing non-loopback dashboard bind without SOVEREIGN_DASHBOARD_TOKEN"
        )

    import uvicorn

    uvicorn.run(create_app(data_dir, mode), host=host, port=port, log_level="info")
