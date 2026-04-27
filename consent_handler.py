"""
consent_handler.py
─────────────────────────────────────────────────────────────────────────────
Detects and interacts with cookie consent banners (CMPs).

This is the most fragile part of the pipeline because consent banners are
wildly inconsistent across sites. The strategy is layered:
  1. Detect known CMP platforms by their DOM fingerprints.
  2. Use platform-specific selectors to click Accept or Reject.
  3. Fall back to keyword-matching on visible button text.
  4. Apply network-level blocking as a belt-and-braces measure regardless.

Every public function returns a status string so callers know how reliable
the result is:
  "handled"   — banner found and interacted with successfully.
  "not_found" — no banner detected (site may not use a CMP, or it was
                already dismissed via a stored cookie from a previous visit —
                shouldn't happen since each context is fresh).
  "failed"    — banner detected but the interaction raised an exception.
─────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
import os
from typing import Optional
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout
from config import THIRD_PARTY_BLOCK_DOMAINS

logger = logging.getLogger(__name__)

import json

AUTOCONSENT_RULES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "lib", "rules.json"
)

# Load rules once at module import time — no per-page overhead
_autoconsent_rules = None


# ─────────────────────────────────────────────────────────────────────────────
# CMP PLATFORM REGISTRY
# Each entry maps a platform name to:
#   selector      — a CSS selector that uniquely identifies the platform's
#                   banner container in the DOM.
#   accept_sel    — selector for the "accept all" button.
#   reject_sel    — selector for the "reject / necessary only" button.
#                   None means the platform requires a multi-step flow
#                   (managed separately in _reject_complex).
#
# Selectors are tested in order; the first match wins. Keep the most common
# platforms at the top for performance.
# ─────────────────────────────────────────────────────────────────────────────

KNOWN_CMPS = {
    "onetrust": {
        "selector":    "#onetrust-banner-sdk",
        "accept_sel":  "#onetrust-accept-btn-handler",
        "reject_sel":  [
            "#onetrust-reject-all-handler",      # Standard banner reject
            ".ot-pc-refuse-all-handler",         # Preference centre variant
            "button.onetrust-close-btn-handler", # Some mobile implementations
        ],
        "reject_complex": False,
    },
    "cookiebot": {
        "selector":    "#CybotCookiebotDialog",
        "accept_sel":  "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "reject_sel":  [
            "#CybotCookiebotDialogBodyButtonDecline",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll",
        ],
        "reject_complex": False,
    },
    "trustarc": {
        "selector":    "#truste-consent-track",
        "accept_sel":  ".truste-button2",       # "Agree and proceed"
        "reject_sel":  ".truste-button1",       # "Disagree and proceed"
        "reject_complex": False,
    },
    "quantcast": {
        "selector":    "[id^='qc-cmp2']",
        "accept_sel":  "button[mode='primary']",
        "reject_sel":  "button[mode='secondary']",
        "reject_complex": False,
    },
    "didomi": {
        "selector":    "#didomi-popup",
        "accept_sel":  "#didomi-notice-agree-button",
        "reject_sel":  "#didomi-notice-disagree-button",
        "reject_complex": False,
    },
    "consentmanager": {
        "selector":    "#cmpbox",
        "accept_sel":  ".cmpboxbtnyes",
        "reject_sel":  ".cmpboxbtnno",
        "reject_complex": False,
    },
    "usercentrics": {
        # Usercentrics renders in a shadow DOM — we target the host element
        "selector":    "uc-cmp-v2, #usercentrics-root",
        "accept_sel":  None,   # Must interact via shadow DOM (handled in _reject_complex)
        "reject_sel":  None,
        "reject_complex": True,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD FALLBACK LISTS
# Used when no known CMP is detected. We search all visible buttons for
# text matching these patterns (case-insensitive, partial match).
# ─────────────────────────────────────────────────────────────────────────────

ACCEPT_KEYWORDS = [
    "accept all", "allow all", "agree to all", "i agree",
    "accept cookies", "allow cookies", "accept & close",
    "got it", "ok", "i accept",
]

REJECT_KEYWORDS = [
    "reject all", "decline all", "refuse all",
    "necessary only", "essential only", "required only",
    "deny", "no, thanks", "continue without accepting",
    "manage preferences",   # Opens a preferences panel — handled by _handle_preferences_panel
]

# ─────────────────────────────────────────────────────────────────────────────
# CMP DETECTION
# ─────────────────────────────────────────────────────────────────────────────

async def _detect_cmp_manual(page: Page) -> Optional[str]:
    """
    Identify which CMP platform a page is using.

    Checks each known CMP's container selector against the live DOM.
    Returns the platform name string (e.g. "onetrust") or None if
    no known platform is detected.
    """
    for name, conf in KNOWN_CMPS.items():
        try:
            # locator.count() is fast — it doesn't wait for the element
            count = await page.locator(conf["selector"]).count()
            if count > 0:
                logger.debug("Detected CMP: %s", name)
                return name
        except Exception:
            continue
    return None

def load_autoconsent_rules() -> list:
    """
    Load the pre-generated CMP rules from the JSON file.
    Returns an empty list if the file doesn't exist — consent handler
    will fall back to manual selectors in that case.
    """
    global _autoconsent_rules
    if _autoconsent_rules is not None:
        return _autoconsent_rules

    if not os.path.exists(AUTOCONSENT_RULES_PATH):
        logger.warning(
            "Autoconsent rules not found at %s. "
            "Run: node generateRules.js",
            AUTOCONSENT_RULES_PATH
        )
        _autoconsent_rules = []
        return _autoconsent_rules

    with open(AUTOCONSENT_RULES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    _autoconsent_rules = data.get("autoconsent", [])
    logger.info("Loaded %d autoconsent CMP rules", len(_autoconsent_rules))
    return _autoconsent_rules


async def detect_cmp_autoconsent(page: Page, timeout_ms: int = 8000) -> Optional[dict]:
    """
    Detect which CMP is present by testing each rule's detectCmp selectors.

    Instead of injecting the autoconsent runtime and calling getState(),
    we run the detection logic ourselves using the exported rules as data.
    Each rule has a list of detectCmp steps — CSS selectors or JS checks
    that identify whether that CMP is active on the page.

    Returns the matching rule dict, or None if no CMP detected.
    """
    rules = load_autoconsent_rules()
    if not rules:
        return None

    # Build one combined JS check that tests all CMPs in a single
    # page.evaluate() call — much faster than one call per rule
    result = await page.evaluate(
        """
        async (rules) => {
            for (const rule of rules) {
                if (!rule.detectCmp || rule.detectCmp.length === 0) continue;

                let detected = true;
                for (const step of rule.detectCmp) {
                    // Each step is either a CSS selector check or a JS eval
                    if (step.exists) {
                        // CSS selector check — element must be present
                        if (!document.querySelector(step.exists)) {
                            detected = false;
                            break;
                        }
                    } else if (step.eval) {
                        // JS expression that must evaluate to truthy
                        try {
                            const result = eval(step.eval);
                            if (!result) {
                                detected = false;
                                break;
                            }
                        } catch (e) {
                            detected = false;
                            break;
                        }
                    }
                }

                if (detected) {
                    return { name: rule.name, rule: rule };
                }
            }
            return null;
        }
        """, rules
    )

    logger.debug("Result from rule search: %s", result)

    if result:
        logger.debug("Autoconsent detected CMP: %s at %s", result["name"], page.url)
    else:
        logger.debug("Autoconsent found no CMP at %s", page.url)

    return result


async def execute_autoconsent_steps(page: Page, steps: list) -> bool:
    """
    Execute a list of autoconsent interaction steps on the page.

    Each step is an action like clicking a selector, evaluating JS,
    or waiting for an element. This is the core of what autoconsent's
    runtime does — we're reimplementing just enough of it to drive
    the opt-in/opt-out flows.
    """
    for step in steps:
        try:
            if step.get("exists"):
                # Just a detection step — skip in execution
                continue

            elif step.get("click"):
                # Click a CSS selector
                sel = step["click"]
                await page.wait_for_selector(sel, state="visible", timeout=1500)
                await page.click(sel)
                await asyncio.sleep(0.1)

            elif step.get("eval"):
                # Execute arbitrary JS
                await page.evaluate(f"() => {{ {step['eval']} }}")

            elif step.get("waitFor"):
                # Wait for a selector to appear
                await page.wait_for_selector(step["waitFor"], timeout=3000)

            elif step.get("wait"):
                # Fixed delay in ms
                await asyncio.sleep(step["wait"] / 1000)

            elif step.get("hide"):
                # Hide an element (used by some CMPs instead of clicking)
                await page.evaluate(f"""
                    () => {{
                        const el = document.querySelector('{step["hide"]}');
                        if (el) el.style.display = 'none';
                    }}
                """)

        except Exception as e:
            logger.debug("Autoconsent step failed (%s): %s", step, e)
            # Non-fatal — continue with remaining steps
            continue

    return True


async def accept_all(page: Page) -> str:
    """
    Accept all cookies — autoconsent rules first, manual fallback second.
    """
    # ── Layer 1: Autoconsent rules ────────────────────────────────────────────
    rule = await detect_cmp_autoconsent(page)
    if rule:
        opt_in_steps = rule.get("rule", {}).get("optIn", [])
        if opt_in_steps:
            success = await execute_autoconsent_steps(page, opt_in_steps)
            if success:
                await _wait_for_banner_dismiss(page)
                logger.debug("Accepted via autoconsent rule: %s", rule["name"])
                return "handled"

    # ── Layer 2: Manual selectors ─────────────────────────────────────────────
    manual_cmp = await _detect_cmp_manual(page)
    if manual_cmp:
        accept_sel = KNOWN_CMPS[manual_cmp].get("accept_sel")
        if accept_sel:
            clicked = await _try_selectors(page, accept_sel)
            if clicked:
                await _wait_for_banner_dismiss(page)
                return "handled"

    # ── Layer 3: Keyword scan ─────────────────────────────────────────────────
    """if not rule and not manual_cmp:
        return 'not_found'"""

    clicked = await _click_by_keyword(page, ACCEPT_KEYWORDS)
    return "handled" if clicked else "failed"


async def reject_non_essential(page: Page) -> str:
    """
    Reject non-essential cookies — autoconsent rules first, manual fallback second.
    """
    # ── Layer 1: Autoconsent rules ────────────────────────────────────────────
    rule = await detect_cmp_autoconsent(page)
    if rule:
        opt_out_steps = rule.get("rule", {}).get("optOut", [])
        if opt_out_steps:
            success = await execute_autoconsent_steps(page, opt_out_steps)
            if success:
                await _wait_for_banner_dismiss(page)
                logger.debug("Rejected via autoconsent rule: %s", rule["name"])
                return "handled"

    # ── Layer 2: Manual selectors ─────────────────────────────────────────────
    manual_cmp = await _detect_cmp_manual(page)
    if manual_cmp:
        conf = KNOWN_CMPS[manual_cmp]
        if conf.get("reject_complex"):
            success = await _reject_complex(page, manual_cmp)
            return "handled" if success else "failed"
        reject_sel = conf.get("reject_sel")
        if reject_sel:
            clicked = await _try_selectors(page, reject_sel)
            if clicked:
                await _wait_for_banner_dismiss(page)
                return "handled"

    # ── Layer 3: Keyword scan ─────────────────────────────────────────────────
    """if not rule and not manual_cmp:
        return 'not_found'"""

    clicked = await _click_by_keyword(page, REJECT_KEYWORDS)
    if clicked:
        if await _is_preferences_panel_open(page):
            await _handle_preferences_panel(page)
        await _wait_for_banner_dismiss(page)
        return "handled"

    return "failed"

async def _reject_complex(page: Page, cmp_name: str) -> bool:
    """
    Handle CMPs that need multi-step interaction to reach necessary-only.
    Currently handles: Usercentrics (shadow DOM).
    """
    if cmp_name == "usercentrics":
        try:
            # Usercentrics renders inside a shadow DOM. We evaluate JS to
            # reach into it and click the "Deny all" button.
            await page.evaluate("""
                () => {
                    const host = document.querySelector('#usercentrics-root');
                    if (!host || !host.shadowRoot) return;
                    const btn = host.shadowRoot.querySelector('[data-testid="uc-deny-all-button"]');
                    if (btn) btn.click();
                }
            """)
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            logger.warning("Usercentrics shadow DOM rejection failed: %s", e)
            return False
    return False


async def _is_preferences_panel_open(page: Page) -> bool:
    """
    Check if clicking "Manage preferences" opened a secondary panel
    rather than dismissing the banner.
    """
    # If the original banner container is still visible, a panel likely opened
    combined = ", ".join(c["selector"] for c in KNOWN_CMPS.values())
    try:
        count = await page.locator(combined).count()
        return count > 0
    except Exception:
        return False


async def _handle_preferences_panel(page: Page):
    """
    In a preferences panel, uncheck all optional categories and save.

    This covers the common "toggle per-category" pattern where the user
    must manually turn off analytics, marketing, etc. individually.
    """
    try:
        # Uncheck all checkboxes that are currently checked
        # Most CMPs mark non-necessary categories as checked by default
        await page.evaluate("""
            () => {
                const checkboxes = document.querySelectorAll(
                    'input[type="checkbox"]:checked:not([disabled])'
                );
                checkboxes.forEach(cb => {
                    // Don't uncheck items labelled as "strictly necessary"
                    const label = cb.closest('label, li')?.textContent?.toLowerCase() || '';
                    const isNecessary = ['necessary', 'essential', 'required', 'strictly'].some(
                        w => label.includes(w)
                    );
                    if (!isNecessary) cb.click();
                });
            }
        """)
        await asyncio.sleep(0.3)

        # Now look for a "Save" / "Confirm" button to commit the preferences
        save_keywords = ["save", "confirm", "save my preferences", "save settings", "apply"]
        await _click_by_keyword(page, save_keywords)

    except Exception as e:
        logger.warning("Preferences panel interaction failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# NETWORK-LEVEL BLOCKING
# Applied independently of UI interaction as a belt-and-braces measure.
# Even if the consent banner wasn't handled, blocking requests at the
# network layer stops third-party cookies from being set.
# ─────────────────────────────────────────────────────────────────────────────

async def apply_network_blocking(page: Page):
    """
    Intercept and abort requests to known third-party tracking domains.

    Uses Playwright's page.route() which is evaluated for every outgoing
    request. If the request URL matches any blocked domain, it's aborted
    before it leaves the browser.

    This function should be called BEFORE page.goto() so blocking is
    active from the very first request.
    """
    async def block_handler(route, request):
        url = request.url.lower()
        # Check if this request's host matches any blocked domain
        if any(domain in url for domain in THIRD_PARTY_BLOCK_DOMAINS):
            await route.abort()
        else:
            await route.continue_()

    # Route all requests through our handler
    await page.route("**/*", block_handler)
    logger.debug("Network blocking active for %d domains", len(THIRD_PARTY_BLOCK_DOMAINS))


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def _try_selectors(page: Page, selectors: list[str] | str, timeout_ms: int = 3000) -> bool:
    """
    Try a list of CSS selectors in order, clicking the first one that appears.

    This replaces _click_and_wait() for cases where a CMP platform has
    multiple known button variants across different site implementations.
    For example, OneTrust uses #onetrust-reject-all-handler on most sites
    but .ot-pc-refuse-all-handler on sites that use the preference centre
    flow — both point to "reject all" but are different elements.

    Parameters:
      selectors  — a single selector string, or a list to try in order
      timeout_ms — how long to wait for each selector before moving to the next

    Returns True if any selector was found and clicked, False if all failed.
    """
    # Normalise to a list so the loop works the same either way
    if isinstance(selectors, str):
        selectors = [selectors]

    for selector in selectors:
        try:
            # Use a short per-selector timeout so we fail fast and move
            # to the next candidate rather than waiting the full duration
            # on each one
            await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
            await page.click(selector)
            await asyncio.sleep(0.3)  # Brief pause for JS handlers to fire
            logger.debug("Clicked selector: %s", selector)
            return True
        except Exception:
            # This selector wasn't found — try the next one
            logger.debug("Selector not found, trying next: %s", selector)
            continue

    logger.debug("No selectors matched from list: %s", selectors)
    return False


async def _click_by_keyword(page: Page, keywords: list[str]) -> bool:
    """
    Find any visible button or link whose text matches a keyword list.

    Evaluates JS to search the DOM rather than using Playwright's
    text selector, which is more reliable for partial matches and
    case-insensitivity across different CMP implementations.

    Returns True if a matching button was found and clicked.
    """
    """found = await page.evaluate('''
        (keywords) => {
            // Gather all clickable elements likely to be consent buttons
            const candidates = [
                ...document.querySelectorAll('button, a[role="button"], [type="submit"]')
            ];
            for (const el of candidates) {
                const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                const matched = keywords.some(k => text.includes(k.toLowerCase()));
                if (matched) {
                    el.click();
                    return true;
                }
            }
            return false;
        }
    ''', keywords)"""
    logger.debug("Attempting keyword click with keywords: %s", keywords)
    elements = await page.query_selector_all('button, a, [role="button"], input[type="button"], span')
    for el in elements:
        try:
            text = await el.inner_text()
            if any(kw in text.lower() for kw in keywords):
                await el.click()
                logger.debug("Clicked element by keyword match: %s", text.strip())
                return True
        except:
            continue
    return False


async def _wait_for_banner_dismiss(page: Page, timeout_ms: int = 3000):
    """
    Wait for the consent banner to disappear from the DOM after clicking.

    We watch for the banner container to become hidden or detached.
    If it doesn't disappear within timeout, we proceed anyway — some CMPs
    keep the container in the DOM but visually hidden.
    """
    combined = ", ".join(c["selector"] for c in KNOWN_CMPS.values())
    try:
        await page.wait_for_selector(combined, state="hidden", timeout=timeout_ms)
    except PlaywrightTimeout:
        # Banner didn't vanish — log but don't fail the whole test
        logger.debug("Banner did not fully dismiss within %dms", timeout_ms)
