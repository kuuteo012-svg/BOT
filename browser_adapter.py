"""Browser submission adapter.

This module owns the Playwright implementation boundary. It intentionally does
not know about API clients, fallback policy, or dashboard persistence.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from browser_site_profiles import SiteProfile, get_site_profile
from logger_setup import logger


_RATE_LIMIT_RE = re.compile(
    r"(rate[\s_-]?limit|too\s+many\s+requests|quota\s+exceeded|\b429\b)",
    re.IGNORECASE,
)


class BrowserResultKind(str, Enum):
    SUCCESS = "SUCCESS"                      # Submit thành công.
    BUSINESS_FAILURE = "BUSINESS_FAILURE"    # Site từ chối code; terminal.
    RATE_LIMITED = "RATE_LIMITED"            # Bị giới hạn; terminal/chờ lâu.
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"  # CDP/timeout; có thể retry.
    UNKNOWN = "UNKNOWN"                      # Không rõ kết quả; retry backoff.


@dataclass(frozen=True)
class BrowserResult:
    kind: BrowserResultKind
    success: bool
    message: str = ""
    elapsed_seconds: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.kind in {
            BrowserResultKind.SUCCESS,
            BrowserResultKind.BUSINESS_FAILURE,
            BrowserResultKind.RATE_LIMITED,
        }


class BrowserDriver(Protocol):
    async def open(self, profile: SiteProfile) -> Any: ...
    async def submit(self, page: Any, profile: SiteProfile, user: str, code: str) -> BrowserResult: ...
    async def close(self) -> None: ...


class BrowserEngineAdapter:
    """Adapter over the existing Edge/CDP engine.

    The engine captures page-bound evidence and returns deferred persistence
    metadata to the caller, which finalizes it after submit locks are released.
    This adapter normalizes only the transport result; it never invokes an
    HTTP/API client.
    """

    def __init__(self, engine: Any):
        self.engine = engine

    async def open(self, profile: SiteProfile) -> Any:
        # Lazy/open is performed by the engine's TabPool during submit. This
        # method exists for the protocol and for future isolated drivers.
        # Tất cả domain hiện dùng chung context "shared" để giữ đúng mô hình
        # Edge/CDP hiện tại. Có thể dùng profile.domain làm key nếu sau này
        # cần tách cookie/fingerprint theo domain.
        return await self.engine.get_or_launch_browser_context("shared")

    async def submit(self, page: Any, profile: SiteProfile, user: str, code: str) -> BrowserResult:
        raise NotImplementedError("Use submit_for_target() for the shared TabPool engine")

    async def submit_for_target(self, user: str, code: str, target_url: str, systems: dict) -> BrowserResult:
        profile = get_site_profile(target_url)
        if profile is None:
            return BrowserResult(BrowserResultKind.TECHNICAL_FAILURE, False, "No browser profile")
        started = time.monotonic()
        try:
            raw = await self.engine.submit_code_browser(user, code, target_url, systems)
        except asyncio.TimeoutError as exc:
            elapsed = time.monotonic() - started
            return BrowserResult(
                BrowserResultKind.TECHNICAL_FAILURE,
                False,
                f"timeout: {exc}",
                elapsed,
            )
        except ValueError as exc:
            elapsed = time.monotonic() - started
            return BrowserResult(
                BrowserResultKind.BUSINESS_FAILURE,
                False,
                f"invalid input: {exc}",
                elapsed,
            )
        except (KeyError, AttributeError, TypeError) as exc:
            logger.exception("Browser adapter internal error")
            elapsed = time.monotonic() - started
            return BrowserResult(
                BrowserResultKind.TECHNICAL_FAILURE,
                False,
                f"internal: {exc}",
                elapsed,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            return BrowserResult(
                BrowserResultKind.TECHNICAL_FAILURE,
                False,
                str(exc),
                elapsed,
            )
        elapsed = time.monotonic() - started
        if raw.get("_infra_failure"):
            return BrowserResult(BrowserResultKind.TECHNICAL_FAILURE, False, str(raw.get("message", "browser transport failure")), elapsed, raw=raw)
        message = str(raw.get("message", ""))
        if raw.get("rate_limited") or _RATE_LIMIT_RE.search(message):
            return BrowserResult(BrowserResultKind.RATE_LIMITED, False, message or "rate limited", elapsed, raw=raw)
        if raw.get("success"):
            return BrowserResult(BrowserResultKind.SUCCESS, True, str(raw.get("message", "")), elapsed, raw=raw)
        if raw.get("is_wrong_code"):
            return BrowserResult(BrowserResultKind.BUSINESS_FAILURE, False, str(raw.get("message", "business failure")), elapsed, raw=raw)
        return BrowserResult(BrowserResultKind.UNKNOWN, False, str(raw.get("message", "unknown browser result")), elapsed, raw=raw)

    async def close(self) -> None:
        await self.engine.cleanup_browsers()


__all__ = ["BrowserDriver", "BrowserEngineAdapter", "BrowserResult", "BrowserResultKind"]
