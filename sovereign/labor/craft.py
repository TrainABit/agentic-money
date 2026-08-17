from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from sovereign.labor.boards import deliverable_text

if TYPE_CHECKING:
    from sovereign.engine.world import World


def produce(world: "World", job: dict[str, Any]) -> dict[str, Any]:
    workdir: Path = world.config.paths().work / job["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    title = str(job.get("title") or "job")
    desc = str(job.get("description") or "")
    blob = f"{title} {desc}".lower()

    readme = (
        f"# {title}\n\n"
        f"Delivered by {world.config.firm_name}.\n\n"
        f"## Brief\n{desc[:800]}\n\n"
        f"## Run\nSee files in this folder. Entry is described below.\n"
    )
    (workdir / "README.md").write_text(readme)
    (workdir / "DELIVERY.md").write_text(deliverable_text(job, world.config.firm_name))

    if any(k in blob for k in ("csv", "data", "excel")):
        (workdir / "clean_csv.py").write_text(_csv_script())
        (workdir / "requirements.txt").write_text("# stdlib only\n")
        entry = "python clean_csv.py input.csv"
    elif any(k in blob for k in ("bot", "telegram", "webhook")):
        (workdir / "alert_bot.py").write_text(_bot_script())
        entry = "python alert_bot.py"
    elif any(k in blob for k in ("landing", "copy", "page")):
        (workdir / "index.html").write_text(_landing(title, world.config.firm_name))
        entry = "open index.html"
    elif any(k in blob for k in ("research", "memo", "brief", "writing")):
        (workdir / "memo.md").write_text(
            f"# {title}\n\nExecutive brief by {world.config.firm_name}.\n\n"
            f"## What changed\n- Scope is the written brief.\n\n## Recommendation\nShip the scoped automation, then retainer.\n"
        )
        entry = "read memo.md"
    else:
        (workdir / "run.py").write_text(_generic_script(title))
        entry = "python run.py"

    if world.config.mode == "live" and world.router.claude.available():
        world.router.complete_in_dir(
            f"You are jailed in this directory. Write or improve the deliverable for:\n{title}\n{desc}\n"
            f"Do not touch files outside this directory. Produce working files.",
            cwd=workdir,
            work_root=world.config.paths().work,
            tier="work",
        )

    dest = world.config.paths().deliveries / job["id"]
    dest.mkdir(parents=True, exist_ok=True)
    for p in workdir.iterdir():
        if p.is_file():
            (dest / p.name).write_bytes(p.read_bytes())
    return {"workdir": str(workdir), "delivery": str(dest), "entry": entry, "files": [p.name for p in dest.iterdir()]}


def _csv_script() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import csv, sys, statistics\n"
        "src = sys.argv[1] if len(sys.argv) > 1 else 'input.csv'\n"
        "with open(src, newline='', encoding='utf-8') as f:\n"
        "    rows = list(csv.DictReader(f))\n"
        "print(f'rows={len(rows)} cols={list(rows[0].keys()) if rows else []}')\n"
        "for k in (rows[0].keys() if rows else []):\n"
        "    vals = []\n"
        "    for r in rows:\n"
        "        try: vals.append(float(r[k]))\n"
        "        except Exception: pass\n"
        "    if vals:\n"
        "        print(k, 'n', len(vals), 'mean', round(statistics.mean(vals), 4))\n"
    )


def _bot_script() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import json, os, urllib.request\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers.get('Content-Length', 0))\n"
        "        body = self.rfile.read(n)\n"
        "        print('alert', body[:500])\n"
        "        self.send_response(204); self.end_headers()\n"
        "    def log_message(self, *args):\n"
        "        return\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    port = int(os.environ.get('PORT', '8088'))\n"
        "    HTTPServer(('0.0.0.0', port), H).serve_forever()\n"
    )


def _landing(title: str, firm: str) -> str:
    return (
        "<!doctype html><meta charset='utf-8'><title>"
        + title
        + "</title><body style='font-family:system-ui;max-width:40rem;margin:4rem auto'>"
        + f"<h1>{title}</h1><p>Draft landing by {firm}. Replace this copy with the offer.</p>"
        + "<p><strong>Price:</strong> fixed. Pay USDC. 48h delivery.</p></body>"
    )


def _generic_script(title: str) -> str:
    return (
        "#!/usr/bin/env python3\n"
        f"print({title!r})\n"
        "print('Deliverable stub — replace with the scoped automation.')\n"
    )
