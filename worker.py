"""
worker.py
─────────────────────────────────────────────────────────────────────────────
Orchestrates one complete A/B cookie test for a single URL.

This is the unit that gets parallelised by runner.py. Each worker:
  1. Launches a browser.
  2. Runs the same test sequence in two isolated contexts (all vs necessary).
  3. Diffs the results.
  4. Writes everything to the database.
  5. Cleans up, regardless of success or failure.

worker.py is intentionally a thin coordinator — the actual browser
manipulation, collection, and diffing happen in the specialist modules.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
from urllib.parse import urlparse

# Global counter for total processed websites
total_processed = 0
counter_lock = asyncio.Lock()
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

import browser_manager
import consent_handler
import collector
import diff as diff_module
import database
import config
from config import RETRY_LIMIT

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE-MODE TEST
# Runs the full load → consent → collect sequence for one cookie mode.
# Called twice by test_site(): once for "all", once for "necessary".
# ─────────────────────────────────────────────────────────────────────────────

async def _run_mode(
    browser,
    url: str,
    url_id: int,
    mode: str,
    origin_host: str,
) -> Optional[dict]:
    """
    Load a URL in a single context (one cookie mode) and collect metrics.

    Returns the metrics dict on success, None on failure.
    The caller is responsible for tearing down whatever context was created.

    Parameters:
      browser     — already-launched Playwright Browser
      url         — the URL to test
      url_id      — database ID (used for screenshot filename)
      mode        — "all" or "necessary"
      origin_host — page hostname (for third-party classification)
    """
    context = None
    try:
        # ── Step 1: Create isolated context and page ─────────────────────────
        # For "necessary" mode, setup_context also attaches network blocking
        # routes before the page is created so no blocked requests slip through.
        context, page, request_log, console_log = await browser_manager.setup_context(
            browser, mode, origin_host
        )

        # ── Step 2: Navigate ──────────────────────────────────────────────────
        # wait_until="networkidle" means we wait for everything to load,
        # including late-firing analytics beacons.
        if url.startswith("http"):
            # URL already has scheme
            success = await browser_manager.navigate_to(page, url)
        else:
            # Try HTTPS first, then HTTP
            url_https = "https://" + url
            logger.debug("[%s] Navigating to %s", mode, url_https)
            success = await browser_manager.navigate_to(page, url_https)
            if not success:
                url_http = "http://" + url
                logger.debug("[%s] Retrying with HTTP: %s", mode, url_http)
                success = await browser_manager.navigate_to(page, url_http)
                if success:
                    url = url_http
            else:
                url = url_https

        if not success:
            logger.error("[%s] Failed to load URL with both HTTP and HTTPS: %s", mode, url)
            return None

        # ── Step 3: Handle consent ────────────────────────────────────────────
        # Interact with the banner based on the mode, then reload the page so
        # subsequent navigation timings reflect the user's post-consent experience.
        if mode == "all":
            consent_status = await consent_handler.accept_all(page)
        else:
            # Apply network blocking BEFORE rejecting so blocked requests are
            # counted appropriately and don't leak in the reload.
            await consent_handler.apply_network_blocking(page)
            consent_status = await consent_handler.reject_non_essential(page)

        # Record which CMP was detected
        cmp = await consent_handler.detect_cmp_autoconsent(page)
        consent_cmp = cmp["name"]

        # Reload the page so saved preferences take effect and measure navigation
        try:
            await page.reload(timeout=config.PAGE_TIMEOUT_MS)
        except Exception:
            logger.debug("[%s] Reload after consent failed, proceeding anyway: %s", mode, url)

        # Wait for the page to re-settle after reload
        await browser_manager.wait_for_settle(page)

        # ── Step 4: Collect all metrics ───────────────────────────────────────
        metrics = await collector.collect_all(
            page=page,
            context=context,
            cookie_mode=mode,
            url_id=url_id,
            request_log=request_log,
            console_log=console_log,
            consent_status=consent_status,
            consent_cmp=consent_cmp,
            origin_host=origin_host,
        )

        logger.info(
            "[%s] %s — load=%.0fms req=%d cookies=%d consent=%s",
            mode, url,
            metrics.get("load_time_ms") or 0,
            metrics.get("request_count") or 0,
            metrics.get("cookie_count") or 0,
            consent_status,
        )
        return metrics

    except PlaywrightTimeout:
        logger.warning("[%s] Timeout at %s", mode, url)
        raise   # Re-raise so test_site() can distinguish timeout from other errors

    except Exception as e:
        logger.error("[%s] Unexpected error at %s: %s", mode, url, e, exc_info=True)
        return None

    finally:
        # ── Step 5: Always clean up ───────────────────────────────────────────
        # Close the context (and its page) regardless of success or failure.
        # Not doing this leaks browser memory — critical in a long-running run.
        await browser_manager.teardown_context(context)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WORKER: FULL A/B TEST FOR ONE URL
# ─────────────────────────────────────────────────────────────────────────────

async def test_site(url: str, url_id: int) -> bool:
    """
    Run the complete two-mode test for a single URL.

    Flow:
      1. Launch one browser (shared by both modes for efficiency).
      2. Run "all cookies" mode → collect metrics_all.
      3. Run "necessary only" mode → collect metrics_nec.
      4. Write both result rows to the database.
      5. Compute diff and write the diff row.
      6. Mark URL as complete.
      7. Close browser.

    Returns True on complete success, False if either mode failed.
    Handles retries and status marking internally.
    """
    origin_host = urlparse(url).hostname or ""
    browser = None

    try:
        # ── Step 1: Launch browser ────────────────────────────────────────────
        # One browser instance hosts both contexts. Launching a browser is
        # expensive (~1s+); sharing it halves that overhead per URL.
        async with async_playwright() as playwright:
            browser = await browser_manager.launch_browser(playwright)

            # ── Step 2: Run both modes ────────────────────────────────────────
            # Run them concurrently to reduce total time per URL.
            metrics_all, metrics_nec = await asyncio.gather(
                _run_mode(browser, url, url_id, "all", origin_host),
                _run_mode(browser, url, url_id, "necessary", origin_host)
            )

            # ── Step 3: Validate ──────────────────────────────────────────────
            # If either mode returned None, something failed. Check retry count
            # and either re-queue or permanently mark as failed.
            if metrics_all is None or metrics_nec is None:
                retry_count = database.increment_retry(url_id)
                if retry_count >= RETRY_LIMIT:
                    database.mark_failed(url_id, "One or both modes failed to collect")
                    logger.warning("Permanently failed (retry limit): %s", url)
                    return False
                else:
                    logger.info("Requeueing for retry (%d/%d): %s", retry_count, RETRY_LIMIT, url)
                    return False

            # ── Step 4: Persist raw results ───────────────────────────────────
            # Write both result rows before computing the diff so if diff
            # calculation crashes, the raw data is still saved.
            diff_result = diff_module.compute_diff(metrics_all, metrics_nec)
            database.write_batch(url_id, metrics_all, metrics_nec, diff_result)
            async with counter_lock:
                global total_processed
                total_processed += 1
                logger.info(
                    "DONE %s | impact=%.2f load_delta=+%.0fms | total_processed=%d",
                    url,
                    diff_result.get("impact_score", 0),
                    diff_result.get("load_time_delta_ms") or 0,
                    total_processed
                )
            return True

    except PlaywrightTimeout:
        # Page didn't load within PAGE_TIMEOUT_MS — a separate status from
        # "failed" because timeouts warrant different follow-up (e.g. the site
        # might just be slow, not broken)
        database.mark_timeout(url_id)
        logger.warning("TIMEOUT: %s", url)
        return False

    except Exception as e:
        # Unexpected exception — mark failed, log full traceback
        database.mark_failed(url_id, str(e)[:500])
        logger.error("FAILED: %s — %s", url, e, exc_info=True)
        return False

    finally:
        # ── Step 7: Always close browser ─────────────────────────────────────
        # Even if test_site() raises, the browser process must be terminated.
        # Orphaned Chrome processes accumulate quickly in a long run.
        if browser:
            await browser_manager.teardown_browser(browser)
