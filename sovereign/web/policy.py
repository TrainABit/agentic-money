"""Safety policy for web automation.

`WebPolicy` fails closed: with no allowlisted domains every navigation is
denied. `requires_human` inspects only `PageState` fields (no network) and
reports when a page appears to need a human (captcha, one-time code, or a
login wall).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:  # avoid a runtime import cycle with sovereign.web.session
    from sovereign.web.session import PageState

# Categories returned by WebPolicy.requires_human.
HUMAN_CAPTCHA = "captcha"
HUMAN_OTP = "otp"
HUMAN_LOGIN_WALL = "login_wall"
HUMAN_REASONS = (HUMAN_CAPTCHA, HUMAN_OTP, HUMAN_LOGIN_WALL)

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_CAPTCHA_TOKENS = ("recaptcha", "hcaptcha", "turnstile", "captcha")
# Phrases scanned across text/title/links; the bare "otp" token is matched only
# against driver-surfaced html_markers to avoid substring false positives.
_OTP_PHRASES = ("verification code", "one-time", "2fa", "authenticator")
_LOGIN_TOKENS = ("sign in", "log in", "login")


def _normalize_domain(entry: str) -> str:
    domain = (entry or "").strip().lower()
    domain = domain.removeprefix("*.")
    return domain.strip(".")


@dataclass(frozen=True)
class WebPolicy:
    allow_domains: tuple[str, ...] = ()
    max_actions: int = 20
    nav_timeout_ms: int = 30000
    block_media: bool = True

    def allows(self, url: str) -> bool:
        """True only for http(s) URLs whose host is under an allowlisted domain."""
        if not self.allow_domains:
            return False  # fail closed
        try:
            parts = urlsplit(url or "")
            host = (parts.hostname or "").rstrip(".").lower()
            if parts.scheme.lower() not in _ALLOWED_SCHEMES or not host:
                return False
            if parts.username is not None or parts.password is not None:
                return False  # credentials-in-URL smells like phishing
        except ValueError:
            return False
        for entry in self.allow_domains:
            domain = _normalize_domain(entry)
            if not domain:
                continue
            if host == domain or host.endswith("." + domain):
                return True
        return False

    def requires_human(self, state: PageState) -> str | None:
        """Reason a human is needed for *state*, or None. Heuristics only."""
        markers = tuple((m or "").lower() for m in (state.html_markers or ()))
        link_blob = " ".join(
            f"{link.get('text') or ''} {link.get('href') or ''}"
            for link in (state.links or ())
        )
        blob = " ".join(
            (
                (state.title or "").lower(),
                (state.text or "").lower(),
                link_blob.lower(),
                " ".join(markers),
            )
        )
        if any(token in blob for token in _CAPTCHA_TOKENS):
            return HUMAN_CAPTCHA
        if any(token in blob for token in _OTP_PHRASES) or "otp" in markers:
            return HUMAN_OTP
        if state.has_password_field and any(token in blob for token in _LOGIN_TOKENS):
            return HUMAN_LOGIN_WALL
        return None
