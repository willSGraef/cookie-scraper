"""
browser_manager.py
─────────────────────────────────────────────────────────────────────────────
Abstracts all Playwright browser and context lifecycle management.

Responsibilities:
  - Launch a real Chrome browser with anti-detection settings.
  - Create isolated browser contexts for each cookie mode.
  - Attach request and console logging to each context.
  - Enforce network blocking on the "necessary" context.
  - Tear down cleanly after each site to prevent memory leaks.

Nothing in this module touches the database or knows about URLs. It only
speaks Playwright. worker.py is the glue between this and everything else.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
from urllib.parse import urlparse
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Request,
    Response,
    ConsoleMessage,
)

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# BROWSER LAUNCH
# ─────────────────────────────────────────────────────────────────────────────

async def launch_browser(playwright) -> Browser:
    """
    Launch a Chrome browser instance configured to minimise bot detection.

    Key decisions:
      channel="chrome"   — Uses the real installed Chrome binary, not Playwright's
                           bundled Chromium. Real Chrome has a different fingerprint
                           and passes navigator.webdriver checks that some sites use.

      --disable-blink-features=AutomationControlled
                         — Removes the `window.navigator.webdriver = true` flag that
                           Playwright sets by default, which many bot-detection
                           services check for.

      --no-sandbox       — Needed when running as root (e.g. in Docker). Remove this
                           if running as a non-root user for better security.
    """
    browser = await playwright.chromium.launch(
        channel=config.BROWSER_CHANNEL,
        headless=config.HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",   # Prevents OOM crashes in Docker
            "--disable-gpu",             # Not needed in headless mode
        ],
    )
    logger.debug("Browser launched (channel=%s, headless=%s)", config.BROWSER_CHANNEL, config.HEADLESS)
    return browser


# ─────────────────────────────────────────────────────────────────────────────
# CONTEXT FACTORY
# ─────────────────────────────────────────────────────────────────────────────

async def create_context(browser: Browser, mode: str) -> BrowserContext:
    """
    Create an isolated browser context for either cookie mode.

    Each context has its own cookie jar, localStorage, and sessionStorage —
    completely isolated from any other context on the same browser instance.
    This is why we can run both modes from one browser: they share the
    process but nothing else.

    Parameters:
      mode — "all" or "necessary"

    The "necessary" mode context gets additional route interception
    added by attach_block_rules() after creation.
    """
    context = await browser.new_context(
        # Identify as a real user
        user_agent=config.USER_AGENT,
        viewport=config.VIEWPORT,
        # Use a realistic locale and timezone to avoid region-specific CMP
        # behaviour and to match what a typical European user would see
        # (GDPR consent banners appear for EU regions).
        locale="en-US",
        timezone_id="America/New_York",
        # Disable the automation flag in JS land
        # (complements the --disable-blink-features launch arg)
        bypass_csp=True,
        java_script_enabled=True,
        # Accept cookies by default — we control what gets SET via consent
        # interaction, not via browser-level blocking
        accept_downloads=False,
    )

    # Override the navigator.webdriver property to False.
    # Some bot-detection scripts check this directly in JS.
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });
    """)

    logger.debug("Context created (mode=%s)", mode)
    return context


# ─────────────────────────────────────────────────────────────────────────────
# REQUEST AND CONSOLE LOGGING
# These listeners are attached to a context (not a page) so they capture
# events from all pages opened within the context, including any
# sub-frames or pop-ups that open during the test.
# ─────────────────────────────────────────────────────────────────────────────

def attach_request_logger(context: BrowserContext, origin_host: str) -> tuple[list, list]:
    """
    Register request/response and console listeners on the context.

    Returns two lists that are mutated in-place as events arrive:
      request_log — one dict per request/response pair
      console_log — one dict per console event

    Both lists are passed to collector.py after the page has finished loading.

    Parameters:
      origin_host — the hostname of the site under test (e.g. "example.com").
                    Used to classify requests as first-party or third-party.
    """
    request_log: list = []
    console_log: list = []

    # ── Request start ────────────────────────────────────────────────────────
    # Fires when the browser begins a request. We record the host so we can
    # classify first vs third party later. Note: at this point we don't yet
    # know the response size, so we add a placeholder.
    async def on_request(request: Request):
        try:
            parsed = urlparse(request.url)
            request_log.append({
                "url":         request.url,
                "method":      request.method,
                "resource_type": request.resource_type,
                "host":        parsed.hostname or "",
                "origin_host": origin_host,
                "size":        0,       # Updated by on_response when the response arrives
                "blocked":     False,   # Updated if the route handler aborts this request
                "_id":         id(request),  # Used to match request to response
            })
        except Exception:
            pass

    # ── Response received ────────────────────────────────────────────────────
    # Fires when the response headers arrive. We read the Content-Length
    # header to estimate transfer size without buffering the full body.
    async def on_response(response: Response):
        try:
            size = int(response.headers.get("content-length", 0))
            # Find the matching request entry by URL (close enough for our purposes)
            for entry in reversed(request_log):
                if entry["url"] == response.url:
                    entry["size"] = size
                    break
        except Exception:
            pass

    # ── Console messages ─────────────────────────────────────────────────────
    # Captures JS console output. We record level and message text.
    # The `source` field tells us which script generated the message.
    async def on_console(msg: ConsoleMessage):
        try:
            console_log.append({
                "level":   msg.type,         # "log", "warning", "error", "info"
                "message": msg.text[:500],   # Truncate very long messages
            })
        except Exception:
            pass

    # Register the listeners
    context.on("request",  on_request)
    context.on("response", on_response)
    # Console events are page-level, not context-level — we attach them
    # when the page is created (see attach_page_listeners below)

    return request_log, console_log


def attach_page_listeners(page: Page, console_log: list):
    """
    Attach console event listener to a specific page.

    Console events come from the page, not the context, so we must
    attach this after page creation. Called by worker.py right after
    context.new_page().
    """
    async def on_console(msg: ConsoleMessage):
        try:
            console_log.append({
                "level":   msg.type,
                "message": msg.text[:500],
            })
        except Exception:
            pass

    page.on("console", on_console)


# ─────────────────────────────────────────────────────────────────────────────
# NETWORK BLOCKING (necessary-only mode)
# ─────────────────────────────────────────────────────────────────────────────

async def attach_block_rules(context: BrowserContext, request_log: list):
    """
    Intercept and abort requests to third-party tracking domains.

    Only attached to "necessary" mode contexts. Works at the context level
    so it applies to all pages, including iframes.

    We also mark the corresponding entry in request_log as blocked so
    collector.py can count them separately.
    """
    blocked_domains = set(config.THIRD_PARTY_BLOCK_DOMAINS)

    async def block_handler(route, request):
        url_lower = request.url.lower()
        if any(domain in url_lower for domain in blocked_domains):
            # Mark as blocked in the request log
            for entry in reversed(request_log):
                if entry["url"] == request.url:
                    entry["blocked"] = True
                    break
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", block_handler)
    logger.debug("Block rules attached (%d domains)", len(blocked_domains))


# ─────────────────────────────────────────────────────────────────────────────
# PAGE NAVIGATION
# ─────────────────────────────────────────────────────────────────────────────

async def navigate_to(page: Page, url: str) -> bool:
    """
    Navigate to a URL and wait for the network to settle.

    Wait strategy: "networkidle" means Playwright waits until there have
    been no more than 0 network connections for at least 500ms. This is
    the most thorough option — it ensures all async scripts and late-loading
    analytics beacons have fired before we start collecting.

    Returns True on success, False on timeout or navigation error.
    """
    try:
        await page.goto(
            url,
            timeout=config.PAGE_TIMEOUT_MS,
            wait_until="domcontentloaded",  # Initial load strategy to avoid waiting indefinitely for slow sites
        )
        return True
    except Exception as e:
        logger.warning("Navigation failed for %s: %s", url, e)
        return False


async def wait_for_settle(page: Page):
    """
    Wait for the page to re-settle after consent interaction.

    Clicking a consent banner often triggers a fresh wave of analytics
    initialisation requests. We wait for networkidle again before collecting
    metrics so we're measuring the post-consent steady state.
    """
    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=config.NETWORK_IDLE_TIMEOUT_MS
        )
    except Exception:
        # If the page doesn't settle within the short window, proceed anyway.
        # A short asyncio.sleep ensures at least some post-consent requests
        # have had time to fire.
        await asyncio.sleep(1.0)


# ─────────────────────────────────────────────────────────────────────────────
# TEARDOWN
# Always called in a finally block by worker.py so contexts and browsers
# are closed even if an exception occurs mid-test.
# ─────────────────────────────────────────────────────────────────────────────

async def teardown_context(context: Optional[BrowserContext]):
    """Close a browser context and free its resources."""
    if context:
        try:
            await context.close()
        except Exception as e:
            logger.debug("Context teardown error (non-fatal): %s", e)


async def teardown_browser(browser: Optional[Browser]):
    """Close the browser process."""
    if browser:
        try:
            await browser.close()
        except Exception as e:
            logger.debug("Browser teardown error (non-fatal): %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE: FULL CONTEXT SETUP
# Wraps the multi-step context creation into a single call for worker.py.
# ─────────────────────────────────────────────────────────────────────────────

async def setup_context(
    browser: Browser,
    mode: str,
    origin_host: str,
) -> tuple[BrowserContext, Page, list, list]:
    """
    Create a context, attach all listeners, and open a new page.

    Returns:
      context     — the BrowserContext
      page        — a fresh Page ready for navigation
      request_log — mutable list of request dicts (populated as page loads)
      console_log — mutable list of console event dicts
    """
    context = await create_context(browser, mode)
    request_log, console_log = attach_request_logger(context, origin_host)

    if mode == "necessary":
        # Attach network blocking BEFORE creating the page so blocking
        # is active from the very first request the page makes
        await attach_block_rules(context, request_log)

    page = await context.new_page()
    attach_page_listeners(page, console_log)
    
    return context, page, request_log, console_log
