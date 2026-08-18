"""Human-handoff login flows for the encrypted web session vault.

First logins, 2FA, and CAPTCHAs are the honest boundary between agent and
web: a human completes them once — headfully, or by importing an exported
storage_state file — and the agent reuses the vaulted session afterward.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def normalize_storage_state(raw: dict) -> dict:
    """Validate an untrusted Playwright storage_state payload.

    Only the documented top-level shape survives: a "cookies" list of
    objects plus an optional "origins" list of objects. Unknown top-level
    keys are stripped; any other shape raises ValueError.
    """
    if not isinstance(raw, dict):
        raise ValueError("storage_state must be a JSON object")
    cookies = raw.get("cookies")
    if not isinstance(cookies, list):
        raise ValueError('storage_state needs a top-level "cookies" list')
    origins = raw.get("origins", [])
    if not isinstance(origins, list):
        raise ValueError('storage_state "origins" must be a list when present')
    if not all(isinstance(cookie, dict) for cookie in cookies):
        raise ValueError("every cookie in storage_state must be an object")
    if not all(isinstance(origin, dict) for origin in origins):
        raise ValueError("every origin in storage_state must be an object")
    return {
        "cookies": [dict(cookie) for cookie in cookies],
        "origins": [dict(origin) for origin in origins],
    }


def import_session_file(vault: Any, domain: str, path: Path) -> dict:
    """Vault a storage_state JSON a human exported; return counts, never values."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {source}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid storage_state JSON: {exc}") from exc
    state = normalize_storage_state(raw)
    vault.save_session(domain, state)
    return {
        "domain": vault._domain_key(domain),
        "cookies": len(state["cookies"]),
        "origins": len(state["origins"]),
    }


def request_web_login(world: Any, service: str, url: str) -> dict:
    """File the one human ask that unlocks a site login (idempotent per service)."""
    if world.human is None:
        raise RuntimeError("world.human is not wired; cannot request a web login")
    where = url or f"https://{service}"
    instruction = (
        f"1) In your own browser, log in to {service} at {where} and complete any "
        f"2FA or CAPTCHA. "
        f"2) Export the session as a Playwright storage_state JSON file (for example "
        f"`playwright codegen --save-storage=state.json {where}` or a cookie-export tool). "
        f"3) Run `sovereign web-login {service} --import state.json` to vault it encrypted. "
        f"Alternatively, on a machine with a display, run "
        f"`sovereign web-login {service} --url {where} --headful` and log in in the "
        f"browser window it opens. "
        f'4) Reply here with {{"done": "1"}}.'
    )
    return world.human.ask(
        service=f"web:{service}",
        instruction=instruction,
        fields=["done"],
        why=(
            "Some sites require a human to log in, complete 2FA, or solve a CAPTCHA "
            "once; the agent reuses the encrypted session afterward."
        ),
    )


def capture_headful_login(
    url: str,
    *,
    driver_factory: Callable[[], Any],
    timeout_s: int = 300,
) -> dict:
    """Open a visible browser, let the human log in, return the storage state.

    The driver is injected so this module never imports playwright; tests
    drive it with a fake. The human paces the login — input() blocks until
    Enter — and the driver is always stopped, even on failure.
    """
    driver = driver_factory()
    try:
        driver.goto(url)
        print(
            f"A browser window is open at {url}.\n"
            f"Log in there, finishing any 2FA or CAPTCHA (you have about {timeout_s}s),\n"
            f"then return to this terminal.",
            flush=True,
        )
        input("Press Enter once you are logged in to capture the session... ")
        state = driver.storage_state()
    finally:
        driver.stop()
    return dict(state)
