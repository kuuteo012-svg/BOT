"""Single source of truth for browser domain profiles and selectors."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class SiteProfile:
    domain: str
    origin: str
    username_selectors: tuple[str, ...]
    code_selectors: tuple[str, ...]
    submit_selector: str
    result_selectors: tuple[str, ...] = ()
    success_markers: tuple[str, ...] = ()
    failure_markers: tuple[str, ...] = ()
    timeout_seconds: float = 5.0
    navigation_timeout_seconds: float = 12.0
    tab_slots: int = 2
    form_settle_seconds: float = 0.03
    result_timeout_ms: int = 5000
    result_poll_interval: float = 0.10
    selector_fast_window_seconds: float = 0.80
    full_reload_every: int = 100

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.domain or "." not in self.domain:
            errors.append("domain is missing or invalid")
        if not self.origin.startswith("https://"):
            errors.append("origin must use https")
        if not self.code_selectors:
            errors.append("code_selectors is empty")
        if not self.submit_selector:
            errors.append("submit_selector is empty")
        if self.timeout_seconds <= 0 or self.navigation_timeout_seconds <= 0:
            errors.append("timeouts must be positive")
        if self.tab_slots < 1 or self.form_settle_seconds < 0:
            errors.append("tab_slots/form_settle_seconds are invalid")
        if self.result_timeout_ms <= 0 or self.result_poll_interval <= 0:
            errors.append("result timing is invalid")
        return errors


_COMMON_SUCCESS = ("thành công", "success", "điểm", "received", "approved")
_COMMON_FAILURE = ("sai", "invalid", "đã dùng", "expired", "not found", "error")

# Domain-specific selectors previously duplicated in browser_engine.py now live
# only here. Generic fallback selectors remain in browser_engine.py because they
# are deliberately shared across sites rather than domain configuration.
SITE_PROFILES: dict[str, SiteProfile] = {
    "tangquaqq88.com": SiteProfile(
        "tangquaqq88.com", "https://tangquaqq88.com",
        ("#account-code", "input[placeholder='Nhập tài khoản của bạn']"),
        ("#promo-code", "input[placeholder='Nhập mã CODE']"),
        "button[type='submit'], form button, img[alt='Kiểm tra ngay']",
        result_selectors=("#promo-code ~ [role='alert']", "form [role='alert']", ".swal2-container", "#toast-container"),
        success_markers=_COMMON_SUCCESS, failure_markers=_COMMON_FAILURE,
        form_settle_seconds=0.02, result_timeout_ms=3500,
        result_poll_interval=0.05, selector_fast_window_seconds=0.50,
        full_reload_every=150,
    ),
    "hi88-freecode.pages.dev": SiteProfile(
        "hi88-freecode.pages.dev", "https://hi88-freecode.pages.dev",
        ("input[placeholder='Nhập tài khoản']",),
        ("input[placeholder='Nhập mã']", "input[autocomplete='one-time-code']"),
        "button[aria-label='Kiểm tra ngay'], button[type='submit'], form button",
        result_selectors=("form [role='alert']", "form [role='status']", ".modal-content", ".toast", "[class*='result' i]"),
        success_markers=_COMMON_SUCCESS, failure_markers=_COMMON_FAILURE,
        form_settle_seconds=0.03, result_timeout_ms=4500,
        result_poll_interval=0.05, selector_fast_window_seconds=0.60,
        full_reload_every=100,
    ),
    "livemm88.net": SiteProfile(
        "livemm88.net", "https://livemm88.net/nhap-code",
        ("#enter-code-username", "#account-code", "input[name='username']"),
        ("#enter-code-code", "input[name='code']", "input[name='giftcode']",
         "input[autocomplete='one-time-code']", "input[placeholder*='mã code' i]",
         "input[placeholder*='nhập mã' i]", "input[id*='code' i]:not(#enter-code-username)",
         "input[id*='promo' i]"),
        "button[aria-label='Kiểm tra'], button:has(img[alt='KIỂM TRA']), button[type='submit'], form button",
        result_selectors=("#enter-code-code ~ [role='alert']", "form [role='alert']", ".swal2-container", ".toast", "[class*='result' i]"),
        success_markers=_COMMON_SUCCESS, failure_markers=_COMMON_FAILURE,
        form_settle_seconds=0.05, result_timeout_ms=4000,
        result_poll_interval=0.05, selector_fast_window_seconds=0.60,
        full_reload_every=150,
    ),
    "rr88code.com": SiteProfile(
        "rr88code.com", "https://rr88code.com",
        ("#username", "#account-code", "input[name='username']", "input[placeholder='Nhập tên người dùng']"),
        ("#code", "input[placeholder='Nhập mã code']"),
        "button:has-text('kiểm tra ngay'), button[type='submit'], form button",
        result_selectors=("form [role='alert']", "form [role='status']", ".swal2-container", ".toast", "[class*='result' i]"),
        success_markers=_COMMON_SUCCESS, failure_markers=_COMMON_FAILURE,
        form_settle_seconds=0.02, result_timeout_ms=3000,
        result_poll_interval=0.05, selector_fast_window_seconds=0.50,
        full_reload_every=200,
    ),
    "xx88code.com": SiteProfile(
        "xx88code.com", "https://xx88code.com",
        ("input[placeholder='Nhập tài khoản']", "#account-code", "input[name='username']"),
        ("input[placeholder='Mã code']", "input[autocomplete='one-time-code']"),
        "button[aria-label='Kiểm tra ngay'], button[type='submit'], form button",
        result_selectors=("form [role='alert']", "form [role='status']", ".swal2-container", ".toast", "[class*='result' i]"),
        success_markers=_COMMON_SUCCESS,
        failure_markers=_COMMON_FAILURE + (
            "code đã sử dụng hết",
            "code đã hết hạn",
            "mã đã được sử dụng",
            "mã đã hết hạn",
            "code used up",
            "code has expired",
        ),
        form_settle_seconds=0.02, result_timeout_ms=3500,
        result_poll_interval=0.05, selector_fast_window_seconds=0.50,
        full_reload_every=200,
    ),
    "gg88live.tv": SiteProfile(
        "gg88live.tv", "https://gg88live.tv/nhap-code",
        ("#enter-code-username",),
        ("#enter-code-code",),
        "button[aria-label='Kiểm tra'], button:has(img[alt='KIỂM TRA']), button[type='submit'], form button",
        result_selectors=("form [role='alert']", "form [role='status']", ".swal2-container", ".toast", "[class*='result' i]"),
        success_markers=_COMMON_SUCCESS, failure_markers=_COMMON_FAILURE,
        form_settle_seconds=0.02, result_timeout_ms=3500,
        result_poll_interval=0.05, selector_fast_window_seconds=0.50,
        full_reload_every=200,
    ),
    "o8code.com": SiteProfile(
        "o8code.com", "https://o8code.com",
        ("input[placeholder='Nhập tên người dùng']", "#account-code", "input[name='username']"),
        ("input[placeholder='Nhập mã code']", "input[autocomplete='one-time-code']"),
        "button[aria-label='Nhận code'], .modal-submit-wrap button, button[type='submit']",
        result_selectors=(".modal-submit-wrap ~ [role='alert']", ".modal [role='alert']", ".swal2-container", ".toast", "[class*='result' i]"),
        success_markers=_COMMON_SUCCESS, failure_markers=_COMMON_FAILURE,
        form_settle_seconds=0.02, result_timeout_ms=3000,
        result_poll_interval=0.05, selector_fast_window_seconds=0.50,
        full_reload_every=200,
    ),
}


def get_site_profile(domain: str) -> SiteProfile | None:
    raw = (domain or "").strip().lower()
    if not raw:
        return None
    if "://" in raw:
        raw = urlparse(raw).hostname or ""
    else:
        raw = raw.split("/", 1)[0]
    raw = raw.removeprefix("www.").strip(".")
    return SITE_PROFILES.get(raw)


def validate_profiles(profiles: Mapping[str, SiteProfile] = SITE_PROFILES) -> dict[str, list[str]]:
    return {domain: errors for domain, profile in profiles.items() if (errors := profile.validate())}


_PROFILE_ERRORS = validate_profiles()
if _PROFILE_ERRORS:
    raise ValueError(f"SITE_PROFILES invalid: {_PROFILE_ERRORS}")


def browser_domains() -> frozenset[str]:
    return frozenset(SITE_PROFILES)


__all__ = ["SiteProfile", "SITE_PROFILES", "get_site_profile", "validate_profiles", "browser_domains"]
