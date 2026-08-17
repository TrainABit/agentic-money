from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

import httpx


SKILLS = (
    "python",
    "javascript",
    "automation",
    "bot",
    "script",
    "data",
    "research",
    "writing",
    "landing",
    "api",
    "scraping",
    "agent",
    "devops",
    "excel",
    "csv",
)


def _id(source: str, title: str, extra: str = "") -> str:
    h = hashlib.sha1(f"{source}:{title}:{extra}".encode()).hexdigest()[:12]
    return f"job_{h}"


_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+")


def extract_email(text: str) -> str | None:
    for m in _EMAIL_RE.finditer(text or ""):
        addr = m.group().rstrip(".,;:")
        if addr.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            continue
        return addr
    return None


def score_job(title: str, description: str = "") -> float:
    text = f"{title} {description}".lower()
    hits = sum(1 for s in SKILLS if s in text)
    return min(1.0, 0.15 + 0.12 * hits)


class JobBoard:
    """Public boards + simulated marketplace. Live fetch is best-effort."""

    def __init__(self, sim: bool = True) -> None:
        self.sim = sim
        self._sim_catalog = _sim_jobs()
        self._live_cache: list[dict[str, Any]] = []
        self._live_at: float = 0.0

    def search(self, tick: int = 0, live: bool = False, include_sim: bool = True) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        if include_sim:
            jobs.extend(self._sim_catalog)
            jobs.extend(_tick_jobs(tick))
        if live:
            jobs.extend(self._live())
        # de-dupe
        seen: set[str] = set()
        out = []
        for j in jobs:
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            j = dict(j)
            j["fit"] = score_job(j.get("title", ""), j.get("description", ""))
            out.append(j)
        out.sort(key=lambda x: x["fit"], reverse=True)
        return out

    def _live(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._live_cache and now - self._live_at < 300:
            return list(self._live_cache)
        found: list[dict[str, Any]] = []
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                r = client.get("https://www.arbeitnow.com/api/job-board-api")
                if r.status_code == 200:
                    data = r.json()
                    for item in data.get("data", [])[:40]:
                        title = item.get("title") or ""
                        desc = (item.get("description") or "")[:1500]
                        url = item.get("url") or ""
                        found.append(
                            {
                                "id": _id("arbeitnow", title, url),
                                "source": "arbeitnow",
                                "title": title,
                                "url": url,
                                "description": desc,
                                "contact": extract_email(desc),
                                "price_usd": 0.0,
                                "status": "open",
                                "remote": True,
                            }
                        )
        except Exception:
            pass
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                r = client.get("https://remoteok.com/api", headers={"User-Agent": "SovereignEngine/0.1"})
                if r.status_code == 200:
                    data = r.json()
                    for item in data:
                        if not isinstance(item, dict) or "position" not in item and "title" not in item:
                            continue
                        title = item.get("position") or item.get("title") or ""
                        if not title:
                            continue
                        found.append(
                            {
                                "id": _id("remoteok", title, item.get("url") or item.get("apply_url") or ""),
                                "source": "remoteok",
                                "title": title,
                                "url": item.get("url") or item.get("apply_url") or "",
                                "description": (item.get("description") or "")[:1500],
                                "contact": extract_email(item.get("description") or "") or item.get("email"),
                                "price_usd": 0.0,
                                "status": "open",
                                "remote": True,
                            }
                        )
        except Exception:
            pass
        self._live_cache = found[:60]
        self._live_at = now
        return list(self._live_cache)


def _sim_jobs() -> list[dict[str, Any]]:
    specs = [
        ("Build a Python CSV cleaner + report", 650, "python csv data"),
        ("Landing page copy for a payroll SaaS", 600, "writing landing"),
        ("Telegram alert bot for inventory", 900, "bot automation python"),
        ("Research brief: EU AI Act for SMBs", 700, "research writing"),
        ("Scrape public RFPs into a sheet", 800, "scraping python api"),
        ("Code review a FastAPI service", 550, "python api"),
        ("Weekly competitor memo (retainer trial)", 800, "research writing retainer"),
        ("Airtable → Slack ops automation", 750, "automation api"),
        ("Solana wallet dashboard snippet", 1200, "javascript api"),
        ("Agent SOP + inbox rules for a shop", 1500, "agent automation"),
    ]
    jobs = []
    for title, price, tags in specs:
        jobs.append(
            {
                "id": _id("sim", title),
                "source": "sim-market",
                "title": title,
                "description": f"Fixed-price gig. Tags: {tags}. Deliver files + short runbook. Pay USDC.",
                "price_usd": float(price),
                "status": "open",
                "remote": True,
                "url": "",
            }
        )
    return jobs


def _tick_jobs(tick: int) -> list[dict[str, Any]]:
    title = f"Automation sprint #{tick}: webhook to spreadsheet"
    return [
        {
            "id": _id("sim-tick", title),
            "source": "sim-market",
            "title": title,
            "description": "python automation api webhook csv",
            "price_usd": 480.0 + (tick % 5) * 40,
            "status": "open",
            "remote": True,
            "url": "",
        }
    ]


def proposal_text(job: dict[str, Any], firm: str, brain_blurb: str) -> str:
    return (
        f"{firm} — scoped delivery\n\n"
        f"Job: {job.get('title')}\n"
        f"Fit notes: {job.get('description', '')[:280]}\n\n"
        f"{brain_blurb}\n\n"
        f"Price: ${job.get('price_usd', 0):.0f} fixed. "
        f"Turnaround: 48h. Payment: USDC on Ethereum/Solana or card. "
        f"Scope is the written brief; extras are a new quote.\n"
        + (f"Apply URL: {job.get('url')}\n" if job.get("url") else "")
    )


def deliverable_text(job: dict[str, Any], firm: str) -> str:
    return (
        f"# Delivery — {job.get('title')}\n\n"
        f"Prepared by {firm}.\n\n"
        f"## Scope completed\n"
        f"- Interpreted the brief: {job.get('description', '')[:400]}\n"
        f"- Produced a runnable artifact and a short runbook.\n\n"
        f"## Runbook\n"
        f"1. Place files from this folder on the target machine.\n"
        f"2. Install deps if a requirements snippet is included.\n"
        f"3. Run the entry command in README.\n\n"
        f"## Acceptance\n"
        f"This closes the fixed-price scope. Further work is a new job.\n"
    )


def sim_client_accepts(job: dict[str, Any], proposal: str, close_rate: float = 1.0) -> bool:
    fit = float(job.get("fit") or 0)
    specific = any(w in proposal.lower() for w in ("scope", "usdc", "48h", "fixed"))
    if not (fit >= 0.35 and specific and len(proposal) > 80):
        return False
    if close_rate >= 1.0:
        return True
    h = int(hashlib.sha1(str(job.get("id")).encode()).hexdigest()[:8], 16)
    return (h % 1000) / 1000.0 < close_rate
