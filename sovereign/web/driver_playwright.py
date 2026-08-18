"""Real browser driver backed by Playwright + headless Chromium.

Playwright is imported lazily inside `start()` so this module can be imported
(and the rest of the package unit-tested) in environments that install only
the core package. Installing the optional extra enables it:

    pip install -e '.[web]' && python -m playwright install chromium
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sovereign.web.session import PageState

_TEXT_CAP = 8000  # driver-side cap; the session re-clamps for extraction
_MAX_LINKS = 40
_MARKER_TOKENS = ("recaptcha", "hcaptcha", "turnstile", "otp", "verification code")
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

_LINKS_JS = (
    "els => els.slice(0, " + str(_MAX_LINKS) + ").map(e => ({"
    "text: (e.innerText || '').trim().slice(0, 200), href: e.href || ''}))"
)


def _block_heavy_resources(route: Any) -> None:
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


class PlaywrightDriver:
    """`BrowserDriver` implementation driving one headless Chromium page."""

    def __init__(self, config: Any) -> None:
        self._config = config
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._pending_storage_state: dict | None = None
        self._last_status: int | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "web automation requires the [web] extra: "
                "pip install -e '.[web]' && python -m playwright install chromium"
            ) from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=bool(getattr(self._config, "headless", True))
        )
        self._open_context(self._pending_storage_state)

    def stop(self) -> None:
        for resource in ("_page", "_context", "_browser", "_pw"):
            handle = getattr(self, resource)
            if handle is None:
                continue
            try:
                if resource == "_pw":
                    handle.stop()
                else:
                    handle.close()
            except Exception:
                pass
            setattr(self, resource, None)

    def _open_context(self, storage_state: dict | None) -> None:
        kwargs: dict[str, Any] = {}
        if storage_state:
            kwargs["storage_state"] = storage_state
        self._context = self._browser.new_context(**kwargs)
        timeout = self._default_timeout()
        self._context.set_default_navigation_timeout(timeout)
        self._context.set_default_timeout(timeout)
        if bool(getattr(self._config, "block_media", True)):
            self._context.route("**/*", _block_heavy_resources)
        self._page = self._context.new_page()
        self._last_status = None

    def _default_timeout(self) -> int:
        return int(getattr(self._config, "nav_timeout_ms", 30000) or 30000)

    def _require_page(self) -> Any:
        if self._page is None:
            raise RuntimeError("PlaywrightDriver not started; call start() first")
        return self._page

    # -- navigation & state --------------------------------------------------

    def goto(self, url: str, *, timeout_ms: int | None = None) -> PageState:
        page = self._require_page()
        timeout = int(timeout_ms) if timeout_ms is not None else self._default_timeout()
        response = page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        self._last_status = response.status if response is not None else None
        return self.state()

    def state(self, *, extract_chars: int = 4000) -> PageState:
        page = self._require_page()
        limit = max(0, min(int(extract_chars), _TEXT_CAP))
        try:
            title = page.title()
        except Exception:
            title = ""
        text = self._visible_text(page)[:limit]
        links = self._links(page)
        try:
            has_password = page.locator("input[type='password']:visible").count() > 0
        except Exception:
            has_password = False
        try:
            content = page.content().lower()
        except Exception:
            content = ""
        markers = tuple(token for token in _MARKER_TOKENS if token in content)
        return PageState(
            url=page.url,
            title=title,
            text=text,
            links=links,
            has_password_field=has_password,
            html_markers=markers,
            status=self._last_status,
        )

    @staticmethod
    def _visible_text(page: Any) -> str:
        try:
            return page.inner_text("body", timeout=2000) or ""
        except Exception:
            try:
                return (
                    page.evaluate("() => document.body ? document.body.innerText : ''")
                    or ""
                )
            except Exception:
                return ""

    @staticmethod
    def _links(page: Any) -> tuple[dict, ...]:
        try:
            raw = page.eval_on_selector_all("a[href]", _LINKS_JS) or []
        except Exception:
            raw = []
        return tuple(
            {"text": str(item.get("text") or ""), "href": str(item.get("href") or "")}
            for item in raw[:_MAX_LINKS]
        )

    # -- interactions (Playwright auto-waits with the context default timeout)

    def click(self, selector: str) -> None:
        self._require_page().click(selector)

    def fill(self, selector: str, value: str) -> None:
        self._require_page().fill(selector, value)

    def press(self, selector: str, key: str) -> None:
        self._require_page().press(selector, key)

    def select_option(self, selector: str, value: str) -> None:
        self._require_page().select_option(selector, value)

    def wait_for(self, selector: str, *, timeout_ms: int | None = None) -> None:
        timeout = int(timeout_ms) if timeout_ms is not None else self._default_timeout()
        self._require_page().wait_for_selector(selector, timeout=timeout)

    def upload(self, selector: str, path: str) -> None:
        self._require_page().set_input_files(selector, path)

    def download(self, trigger_selector: str, dest_dir: str) -> str:
        page = self._require_page()
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        with page.expect_download(timeout=self._default_timeout()) as pending:
            page.click(trigger_selector)
        download = pending.value
        target = dest / (download.suggested_filename or "download.bin")
        download.save_as(str(target))
        return str(target)

    # -- storage & artifacts --------------------------------------------------

    def storage_state(self) -> dict:
        if self._context is None:
            return dict(self._pending_storage_state or {})
        return self._context.storage_state()

    def add_storage_state(self, state: dict) -> None:
        payload = dict(state or {})
        if not payload:
            return
        self._pending_storage_state = payload
        if self._browser is None:
            return  # applied when start() opens the context
        # Recreate the (single) context so cookies AND localStorage apply.
        for handle in (self._page, self._context):
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
        self._open_context(payload)

    def screenshot(self, path: str) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._require_page().screenshot(path=str(target), full_page=True)
        return str(target)
