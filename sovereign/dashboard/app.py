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
  header { padding: 20px 28px; border-bottom: 1px solid #1e2630; display:flex; justify-content:space-between; }
  h1 { font-size: 18px; margin: 0; letter-spacing: .04em; }
  main { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 20px 28px 40px; }
  section { background: #12171d; border: 1px solid #1e2630; border-radius: 12px; padding: 16px 18px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .12em; color: #8ea0b3; margin: 0 0 10px; }
  .bar { height: 8px; background: #1e2630; border-radius: 99px; overflow: hidden; margin: 6px 0 12px; }
  .bar > i { display:block; height:100%; background: linear-gradient(90deg, #3dd6c6, #7cff6b); }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  pre { white-space: pre-wrap; max-height: 280px; overflow: auto; color: #c5d0dc; }
  .ok { color: #7cff6b; } .warn { color: #ffcc66; }
  ul { margin: 0; padding-left: 18px; }
</style>
<header>
  <h1>SOVEREIGN <span id="firm" style="color:#8ea0b3;font-weight:500"></span></h1>
  <div id="meta"></div>
</header>
<main>
  <section>
    <h2>Goals</h2>
    <div id="goals"></div>
    <h2>Treasury</h2>
    <pre id="treas"></pre>
  </section>
  <section>
    <h2>Wallets</h2>
    <pre id="wallet"></pre>
    <h2>Human inbox (logins only)</h2>
    <pre id="inbox"></pre>
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
  document.getElementById('meta').innerHTML = s.cognition.provider + ' · tokens ' + s.cognition.tokens;
  const g = s.goals;
  document.getElementById('goals').innerHTML = [
    ['Minimum $'+g.minimum, g.progress_min],
    ['Recommended $'+g.recommended, g.progress_rec],
    ['Good $'+g.good, g.progress_good],
  ].map(([l,p]) => `<div>${l} — $${g.run_rate_usd.toFixed(0)}</div><div class="bar"><i style="width:${(p*100).toFixed(1)}%"></i></div>`).join('');
  document.getElementById('treas').textContent = JSON.stringify(s.ledger, null, 2);
  document.getElementById('wallet').textContent = JSON.stringify(s.wallet, null, 2);
  document.getElementById('inbox').textContent = JSON.stringify(s.human_inbox, null, 2);
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

    return app


def serve(data_dir: str, mode: str, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(create_app(data_dir, mode), host=host, port=port, log_level="info")
