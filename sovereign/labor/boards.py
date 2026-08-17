from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Self

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

MAX_SOURCE_ITEMS = 40
MAX_LIVE_JOBS = 60
LIVE_CACHE_SECONDS = 300


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

    def __init__(self, sim: bool = True, client: httpx.Client | None = None) -> None:
        self.sim = sim
        self._sim_catalog = _sim_jobs()
        self._live_cache: list[dict[str, Any]] = []
        self._live_at: float = 0.0
        self._client = client
        self._owns_client = client is None
        self._fetch_errors: list[dict[str, Any]] = []
        self._source_counts: dict[str, int] = {}

    @property
    def fetch_errors(self) -> list[dict[str, Any]]:
        return [dict(error) for error in self._fetch_errors]

    @property
    def source_counts(self) -> dict[str, int]:
        return dict(self._source_counts)

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

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

    def search_with_metadata(
        self,
        tick: int = 0,
        live: bool = False,
        include_sim: bool = True,
    ) -> dict[str, Any]:
        """Search without changing role-facing results, while exposing fetch health."""
        jobs = self.search(tick=tick, live=live, include_sim=include_sim)
        return {
            "jobs": jobs,
            "fetch": {
                "errors": self.fetch_errors,
                "source_counts": self.source_counts,
                "fetched_at": self._live_at or None,
            },
        }

    def _http_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            )
        return self._client

    def _record_fetch_error(self, source: str, exc: Exception) -> None:
        error: dict[str, Any] = {
            "source": source,
            "error_type": type(exc).__name__,
            "message": str(exc)[:300] or type(exc).__name__,
        }
        if isinstance(exc, httpx.HTTPStatusError):
            error["status_code"] = exc.response.status_code
        self._fetch_errors.append(error)

    def _live(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._live_at > 0 and now - self._live_at < LIVE_CACHE_SECONDS:
            return list(self._live_cache)
        self._fetch_errors = []
        self._source_counts = {}
        found: list[dict[str, Any]] = []
        client = self._http_client()
        for source, fetcher in (
            ("arbeitnow", self._fetch_arbeitnow),
            ("remoteok", self._fetch_remoteok),
        ):
            try:
                source_jobs = fetcher(client)
            except Exception as exc:  # noqa: BLE001 - isolate independent external sources
                self._record_fetch_error(source, exc)
                source_jobs = []
            self._source_counts[source] = len(source_jobs)
            found.extend(source_jobs)
        self._live_cache = found[:MAX_LIVE_JOBS]
        self._live_at = now
        return list(self._live_cache)

    def _fetch_arbeitnow(self, client: httpx.Client) -> list[dict[str, Any]]:
        response = client.get("https://www.arbeitnow.com/api/job-board-api")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise TypeError("expected an object with a data list")
        found = []
        for item in data["data"][:MAX_SOURCE_ITEMS]:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or ""
            if not title:
                continue
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
        return found

    def _fetch_remoteok(self, client: httpx.Client) -> list[dict[str, Any]]:
        response = client.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "SovereignEngine/0.1"},
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise TypeError("expected a list response")
        found = []
        for item in data[:MAX_SOURCE_ITEMS]:
            if not isinstance(item, dict) or ("position" not in item and "title" not in item):
                continue
            title = item.get("position") or item.get("title") or ""
            if not title:
                continue
            url = item.get("url") or item.get("apply_url") or ""
            description = (item.get("description") or "")[:1500]
            found.append(
                {
                    "id": _id("remoteok", title, url),
                    "source": "remoteok",
                    "title": title,
                    "url": url,
                    "description": description,
                    "contact": extract_email(description) or item.get("email"),
                    "price_usd": 0.0,
                    "status": "open",
                    "remote": True,
                }
            )
        return found


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
