"""
collector.py
─────────────────────────────────────────────────────────────────────────────
Captures all metrics from a loaded page after consent has been handled.

This module has no side effects outside the browser context it receives —
it never writes to disk directly (except screenshots, to the path it's
told to use), and never touches the database. It just collects and returns
a dict of metrics for the caller (worker.py) to persist.

All collection functions degrade gracefully: if one metric fails to collect,
the rest still run. The failing field is set to None rather than crashing
the whole test run.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import json
import logging
import os
import re
from typing import Optional

from playwright.async_api import Page, BrowserContext

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PERFORMANCE TIMING
# Reads from the browser's built-in Performance API.
# ─────────────────────────────────────────────────────────────────────────────

async def collect_performance_timing(page: Page) -> dict:
    """
    Extract navigation timing metrics from window.performance.

    This version selects the latest navigation entry (useful when the page
    has been reloaded after consent interaction) and returns primary user-
    visible metrics (ttfb and domcontentloaded). load_time_ms and lcp_ms are
    retained but the emphasis is on ttfb_ms and dom_loaded_ms.
    """
    try:
        timing = await page.evaluate("""
            () => {
                const navEntries = performance.getEntriesByType('navigation');
                if (!navEntries || navEntries.length === 0) return null;
                const nav = navEntries[navEntries.length - 1];
                return {
                    ttfb_ms:       nav.responseStart - nav.requestStart,
                    dom_loaded_ms: nav.domContentLoadedEventEnd - nav.startTime,
                    load_time_ms:  (nav.loadEventEnd && nav.loadEventEnd - nav.startTime) || null,
                };
            }
        """)

        # LCP may not always be available. Use the last reported value if present.
        lcp = await page.evaluate("""
            () => {
                const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
                if (!lcpEntries || lcpEntries.length === 0) return null;
                return lcpEntries[lcpEntries.length - 1].startTime;
            }
        """)

        if timing:
            timing["lcp_ms"] = lcp
        return timing or {}

    except Exception as e:
        logger.warning("Performance timing collection failed: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# NETWORK STATS
# Reads from the request log that browser_manager attached to the context.
# ─────────────────────────────────────────────────────────────────────────────

async def collect_network_stats(request_log: list) -> dict:
    """
    Summarise network activity from the request log.

    browser_manager.attach_request_logger() registers listeners on the
    context that append to `request_log` for every request/response pair.
    We receive that list here and compute summary statistics.

    Returns:
      request_count       — total number of HTTP requests made
      bytes_transferred   — total response body bytes (approximate)
      blocked_count       — requests that were aborted (necessary-only mode)
      third_party_domains — unique non-origin hostnames contacted
    """
    try:
        if not request_log:
            return {
                "request_count": 0,
                "bytes_transferred": 0,
                "blocked_count": 0,
                "third_party_domains": [],
            }

        request_count     = len(request_log)
        bytes_transferred = sum(r.get("size", 0) for r in request_log)
        blocked_count     = sum(1 for r in request_log if r.get("blocked", False))

        # Extract unique third-party hostnames
        # "third party" = any host that differs from the page's own origin
        third_party = set()
        for r in request_log:
            host = r.get("host", "")
            origin = r.get("origin_host", "")
            if host and origin and host != origin:
                third_party.add(host)

        return {
            "request_count":     request_count,
            "bytes_transferred": bytes_transferred,
            "blocked_count":     blocked_count,
            "third_party_domains": sorted(third_party),
        }

    except Exception as e:
        logger.warning("Network stats collection failed: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# CONSOLE ERRORS
# ─────────────────────────────────────────────────────────────────────────────

async def collect_console_errors(console_log: list) -> dict:
    """
    Filter the console event log down to errors and warnings.

    browser_manager.attach_request_logger() also collects console events.
    We filter for 'error' and 'warning' levels only — 'log' and 'info'
    are too noisy and rarely indicate functionality problems.

    Returns:
      console_error_count — number of errors/warnings
      console_errors      — list of {level, message} dicts
    """
    try:
        filtered = [
            {"level": e["level"], "message": e["message"][:500]}
            for e in console_log
            if e.get("level") in ("error", "warning")
        ]
        return {
            "console_error_count": len(filtered),
            "console_errors":      filtered,
        }
    except Exception as e:
        logger.warning("Console error collection failed: %s", e)
        return {"console_error_count": 0, "console_errors": []}


# ─────────────────────────────────────────────────────────────────────────────
# DOM SIGNALS
# Checks whether expected page elements are present after load.
# ─────────────────────────────────────────────────────────────────────────────

async def collect_dom_signals(page: Page) -> dict:
    """
    Run all DOM presence checks defined in config.DOM_SIGNAL_CHECKS.

    Each check is a {name, selector} dict. We test each selector against
    the live DOM and record True/False. When we diff the two modes, a
    signal that is True in all-cookies mode and False in necessary-only
    mode indicates a functionality regression.

    Returns a flat dict: {"video_embed": True, "chat_widget": False, ...}
    """
    if not config.COLLECT_DOM_SIGNALS:
        return {}

    results = {}
    for check in config.DOM_SIGNAL_CHECKS:
        name     = check["name"]
        selector = check["selector"]
        try:
            # count() is non-blocking and doesn't wait for the element
            count = await page.locator(selector).count()
            results[name] = count > 0
        except Exception as e:
            logger.debug("DOM signal check '%s' failed: %s", name, e)
            results[name] = None   # None = check errored, not the same as False

    return results


# ─────────────────────────────────────────────────────────────────────────────
# COOKIE INVENTORY
# ─────────────────────────────────────────────────────────────────────────────

# Cookie risk tiers based on attributes instead of name matching.
# Tiers: Minimal-Risk, Low-Risk, Moderate-Risk, Critical-Risk.


def _categorise_cookie_by_attributes(cookie: dict, origin_host: str) -> str:
    """
    Classify a cookie into a risk tier using four attributes:
      - first_party (domain matches origin_host)
      - session (no persistent expiry)
      - http_only
      - secure

    Minimal-Risk: all four are True (First-Party, Session, HTTPOnly, Secure)
    Critical-Risk: all four are the inverse (Third-Party, Persistent, not HTTPOnly, not Secure)
    Low-Risk: matches 3/4 attributes of Minimal-Risk
    Moderate-Risk: matches 3/4 attributes of Critical-Risk
    """
    domain = (cookie.get("domain") or "").lstrip('.')
    origin = (origin_host or "").lower()

    # First-party: origin endswith domain (handles subdomains)
    first_party = False
    try:
        if domain and origin:
            first_party = origin.endswith(domain.lower()) or domain.lower().endswith(origin)
    except Exception:
        first_party = False

    # Session cookie: expires is falsy or -1/0 -> session, else persistent
    expires = cookie.get("expires")
    session = expires in (None, 0, -1)

    http_only = bool(cookie.get("httpOnly"))
    secure = bool(cookie.get("secure"))

    minimal_attrs = [first_party, session, http_only, secure]
    critical_attrs = [not first_party, not session, not http_only, not secure]

    # Count matches
    minimal_matches = sum(1 for v in minimal_attrs if v)
    critical_matches = sum(1 for v in critical_attrs if v)

    if minimal_matches == 4:
        return "Minimal-Risk"
    if critical_matches == 4:
        return "Critical-Risk"
    if minimal_matches >= 3:
        return "Low-Risk"
    if critical_matches >= 3:
        return "Moderate-Risk"
    # Fallback
    return "Moderate-Risk"


async def collect_cookies(context: BrowserContext, origin_host: str) -> dict:
    """
    Retrieve all cookies set in this browser context after page load and
    classify them into risk tiers based on attributes.

    Returns:
      cookie_count — total number of cookies
      cookies      — list of enriched cookie dicts (includes risk_tier)
    """
    if not config.COLLECT_COOKIE_INVENTORY:
        return {"cookie_count": 0, "cookies": []}

    try:
        raw_cookies = await context.cookies()
        enriched = []
        for c in raw_cookies:
            risk = _categorise_cookie_by_attributes(c, origin_host)
            enriched.append({
                "name":      c.get("name"),
                "domain":    c.get("domain"),
                "path":      c.get("path"),
                "secure":    c.get("secure"),
                "http_only": c.get("httpOnly"),
                "same_site": c.get("sameSite"),
                "risk_tier": risk,
            })
        return {
            "cookie_count": len(enriched),
            "cookies":      enriched,
        }
    except Exception as e:
        logger.warning("Cookie collection failed: %s", e)
        return {"cookie_count": 0, "cookies": []}


# ─────────────────────────────────────────────────────────────────────────────
# SCREENSHOT
# ─────────────────────────────────────────────────────────────────────────────

async def take_screenshot(page: Page, url_id: int, cookie_mode: str) -> Optional[str]:
    """
    Capture a full-page screenshot and save it to disk.

    File naming: screenshots/<url_id>_<mode>.png
    Returns the file path on success, None on failure.
    """
    if not config.COLLECT_SCREENSHOTS:
        return None

    filename = f"{url_id}_{cookie_mode}.png"
    filepath = os.path.join(config.SCREENSHOTS_DIR, filename)

    try:
        # full_page=True scrolls and stitches the entire page, not just
        # the visible viewport. This is important for visual diffing.
        await page.screenshot(path=filepath, full_page=True)
        logger.debug("Screenshot saved: %s", filepath)
        return filepath
    except Exception as e:
        logger.warning("Screenshot failed for url_id=%d mode=%s: %s", url_id, cookie_mode, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MASTER COLLECTOR
# Orchestrates all individual collectors and assembles one result dict.
# ─────────────────────────────────────────────────────────────────────────────

async def collect_all(
    page: Page,
    context: BrowserContext,
    cookie_mode: str,
    url_id: int,
    request_log: list,
    console_log: list,
    consent_status: str,
    consent_cmp: Optional[str],
    origin_host: str,
) -> dict:
    """
    Run all collection functions and merge into a single result dict.

    Parameters:
      page           — the Playwright page (after load + consent handling)
      context        — the browser context (for cookie access)
      cookie_mode    — "all" or "necessary" (for screenshot naming)
      url_id         — database ID (for screenshot naming)
      request_log    — list populated by browser_manager's request listener
      console_log    — list populated by browser_manager's console listener
      consent_status — outcome string from consent_handler
      consent_cmp    — detected CMP name (or None)

    Returns:
      A single flat dict merging all metric groups. Any group that fails
      to collect contributes None values rather than raising.
    """
    # Run independent collectors concurrently where possible.
    # collect_cookies and performance_timing don't interact with the page
    # and can run in parallel with each other.
    performance, network, console, dom, cookies, screenshot_path = await asyncio.gather(
        collect_performance_timing(page),
        collect_network_stats(request_log),
        collect_console_errors(console_log),
        collect_dom_signals(page),
        collect_cookies(context, origin_host),
        take_screenshot(page, url_id, cookie_mode),
        return_exceptions=False,   # Individual failures are handled inside each fn
    )

    # Merge all dicts into one flat result
    result = {
        # Source metadata
        "cookie_mode":     cookie_mode,
        "consent_status":  consent_status,
        "consent_cmp":     consent_cmp,
        "screenshot_path": screenshot_path,
    }
    result.update(performance or {})
    result.update(network or {})
    result.update(console or {})
    result["dom_signals"] = dom or {}
    result.update(cookies or {})

    logger.debug(
        "Collected metrics for url_id=%d mode=%s | load=%.0fms req=%d cookies=%d errors=%d",
        url_id,
        cookie_mode,
        result.get("load_time_ms") or 0,
        result.get("request_count") or 0,
        result.get("cookie_count") or 0,
        result.get("console_error_count") or 0,
    )

    return result
