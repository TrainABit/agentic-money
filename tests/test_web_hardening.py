"""Regressions for the web allowlist-escape and credential-redaction fixes."""
import pytest

from sovereign.web.policy import WebPolicy
from sovereign.web.session import (
    BrowserSession,
    PageState,
    WebPolicyError,
    redact_url,
)


class _StubDriver:
    """Minimal BrowserDriver whose current URL is fully controllable."""

    def __init__(self) -> None:
        self._url = "about:blank"
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def _state(self) -> PageState:
        return PageState(
            url=self._url,
            title="t",
            text="body",
            links=(),
            has_password_field=False,
            html_markers=(),
            status=200,
        )

    def goto(self, url, *, timeout_ms=0):
        self._url = url
        return self._state()

    def state(self, *, extract_chars=4000):
        return self._state()

    def click(self, selector):
        # Simulate an in-page navigation to an off-allowlist host.
        self._url = "https://evil.com/steal"

    def fill(self, selector, value):
        pass

    def press(self, selector, key="Enter"):
        pass

    def select_option(self, selector, value):
        pass

    def wait_for(self, selector, *, timeout_ms=0):
        pass

    def upload(self, selector, path):
        pass

    def download(self, selector, dest_dir):
        return f"{dest_dir}/file"

    def storage_state(self):
        return {}

    def add_storage_state(self, state):
        pass

    def screenshot(self, path):
        return path


def _policy() -> WebPolicy:
    return WebPolicy(
        allow_domains=("example.com",),
        max_actions=10,
        nav_timeout_ms=1000,
        block_media=True,
    )


def test_redact_url_strips_userinfo():
    assert redact_url("https://user:s3cret@example.com/x?y=1") == "https://example.com/x?y=1"
    assert redact_url("http://bob:pw@host:8080/p") == "http://host:8080/p"
    assert "s3cret" not in redact_url("https://user:s3cret@example.com/")


def test_credentials_in_url_denied_without_leaking_password():
    session = BrowserSession(_StubDriver(), _policy())
    with pytest.raises(WebPolicyError) as exc:
        session.navigate("https://agent:hunter2pw@example.com/")
    message = str(exc.value)
    assert "hunter2pw" not in message
    # And nothing about the secret reaches the in-memory action log either.
    assert all("hunter2pw" not in target for _, target, _ in session.action_log)


def test_click_that_leaves_allowlist_is_blocked():
    session = BrowserSession(_StubDriver(), _policy())
    session.navigate("https://example.com/")
    with pytest.raises(WebPolicyError):
        session.click("#go")  # stub navigates to evil.com


def test_redirect_landing_off_allowlist_is_blocked():
    driver = _StubDriver()
    session = BrowserSession(driver, _policy())
    session.navigate("https://example.com/")
    driver._url = "https://evil.com/phish"  # emulate a redirect landing off-list
    with pytest.raises(WebPolicyError):
        session.extract()
    with pytest.raises(WebPolicyError):
        session.snapshot(allow_human_gate=False)
