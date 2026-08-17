from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap

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
</style>
<header>
  <h1>SOVEREIGN <span id="firm" style="color:#8ea0b3;font-weight:500"></span></h1>
  <div id="meta"></div>
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
  <section style="grid-column: 1 / -1">
    <h2>Recent events</h2>
    <pre id="events"></pre>
  </section>
</main>
<script>
async function tick(){
  const s = await (await fetch('/api/status')).json();
  document.getElementById('firm').textContent = s.firm + ' · ' + s.mode + ' · tick ' + s.tick;
  document.getElementById('meta').textContent = (s.cognition.provider + ' · tokens ' + s.cognition.tokens);
  const g = s.goals;
  document.getElementById('goals').innerHTML = [
    ['Minimum $'+g.minimum, g.progress_min],
    ['Recommended $'+g.recommended, g.progress_rec],
    ['Good $'+g.good, g.progress_good],
  ].map(([l,p]) => `<div>${l} — trailing $${g.run_rate_usd.toFixed(0)} / life $${(g.lifetime_usd||0).toFixed(0)}</div><div class="bar"><i style="width:${(p*100).toFixed(1)}%"></i></div>`).join('');
  document.getElementById('pills').innerHTML = Object.entries(s.pipeline||{}).map(([k,v]) => `<span class="pill">${k} ${v}</span>`).join('') +
    `<span class="pill">invoices open ${s.invoices_open}</span><span class="pill">mail out ${s.mail_out}</span>`;
  document.getElementById('treas').textContent = JSON.stringify(s.ledger, null, 2);
  document.getElementById('wallet').textContent = JSON.stringify({wallet:s.wallet, credentials:s.credentials_present}, null, 2);
  document.getElementById('inbox').textContent = JSON.stringify(s.human_inbox, null, 2);
  document.getElementById('inv').textContent = JSON.stringify({open:s.invoices_open, paid:s.invoices_paid, offers:s.offers}, null, 2);
  document.getElementById('strat').textContent = JSON.stringify({certified:s.certified_strategies, rejected:s.rejected_strategies}, null, 2);
  document.getElementById('agents').textContent = JSON.stringify({frozen:s.frozen_agents, reputation:s.reputation, broker:s.broker}, null, 2);
  document.getElementById('events').textContent = JSON.stringify(s.recent_events.slice(0,12), null, 2);
}
tick(); setInterval(tick, 3000);
</script>
</html>
"""


def create_app(data_dir: str, mode: str) -> FastAPI:
    app = FastAPI(title="Sovereign", docs_url=None, redoc_url=None)

    def world():
        return bootstrap(EngineConfig(mode=mode, data_dir=Path(data_dir)))  # type: ignore[arg-type]

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return HTML

    @app.get("/api/status")
    def status() -> JSONResponse:
        return JSONResponse(world().status())

    @app.get("/api/invoices")
    def invoices() -> JSONResponse:
        return JSONResponse(world().store.invoices())

    @app.get("/api/jobs")
    def jobs() -> JSONResponse:
        return JSONResponse(world().store.jobs())

    return app


def serve(data_dir: str, mode: str, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(create_app(data_dir, mode), host=host, port=port, log_level="info")
