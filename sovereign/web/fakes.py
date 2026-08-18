"""Deterministic in-memory `BrowserDriver` for unit tests.

No network, no playwright. Pages are scripted as::

    {url: {"title": str, "text": str, "links": [{"text","href"}, ...],
           "password": bool, "markers": [str, ...],
           "next": {selector: url}, "status": int}}

`fields` records every filled selector -> value so tests can assert a secret
was delivered without it ever appearing in an action log. `actions` records
driver-level calls (never values).
"""

from __future__ import annotations

from pathlib import Path

from sovereign.web.session import PageState


class FakeDriver:
    def __init__(
        self,
        pages: dict[str, dict] | None = None,
        *,
        storage: dict | None = None,
    ) -> None:
        self.pages = dict(pages or {})
        self.fields: dict[str, str] = {}
        self.actions: list[tuple] = []
        self.started = False
        self.stopped = False
        self._storage: dict = dict(storage or {})
        self._current: str | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self.started = True
        self.actions.append(("start",))

    def stop(self) -> None:
        self.stopped = True
        self.actions.append(("stop",))

    # -- navigation & state ---------------------------------------------------

    def goto(self, url: str, *, timeout_ms: int | None = None) -> PageState:
        self.actions.append(("goto", url))
        self._current = url  # navigation happens even to unscripted pages
        return self._state_for(url)

    def state(self, *, extract_chars: int = 4000) -> PageState:
        if self._current is None:
            return PageState(url="about:blank", title="", text="", status=None)
        return self._state_for(self._current, extract_chars=extract_chars)

    def _state_for(self, url: str, *, extract_chars: int = 4000) -> PageState:
        spec = self.pages.get(url)
        if spec is None:
            return PageState(
                url=url,
                title="Not Found",
                text="404 page not found",
                links=(),
                has_password_field=False,
                html_markers=(),
                status=404,
            )
        return PageState(
            url=url,
            title=str(spec.get("title", "")),
            text=str(spec.get("text", ""))[: max(0, int(extract_chars))],
            links=tuple(dict(link) for link in spec.get("links", ())),
            has_password_field=bool(spec.get("password", False)),
            html_markers=tuple(spec.get("markers", ())),
            status=int(spec.get("status", 200)),
        )

    # -- interactions -----------------------------------------------------------

    def click(self, selector: str) -> None:
        self.actions.append(("click", selector))
        spec = self.pages.get(self._current or "")
        if spec:
            target = dict(spec.get("next", {})).get(selector)
            if target is not None:
                self._current = target

    def fill(self, selector: str, value: str) -> None:
        self.actions.append(("fill", selector))  # value goes to fields only
        self.fields[selector] = value

    def press(self, selector: str, key: str) -> None:
        self.actions.append(("press", selector, key))

    def select_option(self, selector: str, value: str) -> None:
        self.actions.append(("select_option", selector))
        self.fields[selector] = value

    def wait_for(self, selector: str, *, timeout_ms: int | None = None) -> None:
        self.actions.append(("wait_for", selector))

    def upload(self, selector: str, path: str) -> None:
        self.actions.append(("upload", selector))
        self.fields[selector] = str(path)

    def download(self, trigger_selector: str, dest_dir: str) -> str:
        self.actions.append(("download", trigger_selector))
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / "download.txt"
        target.write_bytes(b"fake-download")
        return str(target)

    # -- storage & artifacts -----------------------------------------------------

    def storage_state(self) -> dict:
        return dict(self._storage)

    def add_storage_state(self, state: dict) -> None:
        self.actions.append(("add_storage_state",))
        self._storage.update(state or {})

    def screenshot(self, path: str) -> str:
        self.actions.append(("screenshot",))
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG-fake-screenshot")
        return str(target)
