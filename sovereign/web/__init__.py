"""Headless web automation with strict safety rails.

Only the core policy/session API is re-exported here. Drivers and sibling
modules (vault, login, fakes, driver_playwright) are imported via full path.
"""

from sovereign.web.policy import (
    HUMAN_CAPTCHA,
    HUMAN_LOGIN_WALL,
    HUMAN_OTP,
    HUMAN_REASONS,
    WebPolicy,
)
from sovereign.web.session import (
    BrowserDriver,
    BrowserSession,
    HumanInterventionRequired,
    PageState,
    WebActionError,
    WebPolicyError,
    WebRuntime,
    default_driver_factory,
)

__all__ = [
    "HUMAN_CAPTCHA",
    "HUMAN_LOGIN_WALL",
    "HUMAN_OTP",
    "HUMAN_REASONS",
    "BrowserDriver",
    "BrowserSession",
    "HumanInterventionRequired",
    "PageState",
    "WebActionError",
    "WebPolicy",
    "WebPolicyError",
    "WebRuntime",
    "default_driver_factory",
]
