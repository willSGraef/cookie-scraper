"""
config.py
─────────────────────────────────────────────────────────────────────────────
Central configuration file for the cookie survey pipeline.

Every other script imports its settings from here. This means you only ever
need to change a value in one place — no hunting through multiple files to
update a timeout or swap a file path.

Usage in other scripts:
    from config import PAGE_TIMEOUT_MS, DB_PATH, MAX_WORKERS
─────────────────────────────────────────────────────────────────────────────
"""

import os

# ─────────────────────────────────────────────────────────────────────────────
# BROWSER SETTINGS
# Controls how Playwright launches and behaves.
# ─────────────────────────────────────────────────────────────────────────────

# Use the real installed Chrome rather than bundled Chromium.
# This reduces bot-detection false positives on sites that fingerprint browsers.
# Requires Google Chrome to be installed on the machine running the pipeline.
BROWSER_CHANNEL = "chrome"

# True = no visible browser window (fast, for production runs).
# False = browser window opens (useful when debugging consent handlers).
# Override at runtime with the --debug flag in runner.py.
HEADLESS = True

# How long (ms) to wait for a page to reach the "networkidle" state before
# giving up and marking the URL as a timeout. 30s is generous but needed for
# slow or cookie-wall-heavy sites.
PAGE_TIMEOUT_MS = 40_000

# After the main load, we wait again after consent interactions because
# clicking "Accept" often triggers a fresh wave of analytics requests.
# This shorter timeout catches that second burst before we collect metrics.
NETWORK_IDLE_TIMEOUT_MS = 5_000

# A realistic modern Chrome UA string. Some sites serve degraded content or
# refuse to load for obvious automation agents.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Viewport to emulate. 1280x800 is a common laptop resolution and avoids
# triggering mobile layouts that behave differently from desktop.
VIEWPORT = {"width": 1280, "height": 800}

# ─────────────────────────────────────────────────────────────────────────────
# CONCURRENCY & RESILIENCE
# Controls how many sites run in parallel and how failures are handled.
# ─────────────────────────────────────────────────────────────────────────────

# Number of sites tested simultaneously. Each worker holds one Chrome instance
# with two contexts (all-cookies and necessary-only), so memory scales linearly.
# On a 16GB machine, 8 workers is a safe starting point. Lower if you see OOM.
MAX_WORKERS = 12

# How many times to retry a URL that failed due to a non-timeout error before
# giving up and marking it permanently failed.
RETRY_LIMIT = 1

# How many URLs to pull from the database in a single batch. The runner loops
# over batches until the queue is empty. Smaller batches = more frequent DB
# writes and progress updates; larger batches = fewer round-trips.
BATCH_SIZE = 100

# Seconds to wait between requests to the same root domain. Prevents a cluster
# of URLs from the same site triggering rate limiting or IP blocks.
SAME_DOMAIN_DELAY_S = 3.0

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

# Path to the SQLite database file. For large-scale runs (10k+ URLs) you may
# want to swap this for a PostgreSQL connection string and update database.py
# to use asyncpg instead of sqlite3.
DB_PATH = os.path.join(os.path.dirname(__file__), "results.db")

# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT PATHS
# ─────────────────────────────────────────────────────────────────────────────

# Root directory where screenshots are saved. Organised as:
#   screenshots/<url_id>_all.png
#   screenshots/<url_id>_necessary.png
SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")

# Where analytics.py writes its CSV tables and chart PNGs.
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")

# Log file for the runner. Appended to across runs so you have a full history.
LOG_FILE = os.path.join(os.path.dirname(__file__), "run.log")

# ─────────────────────────────────────────────────────────────────────────────
# COOKIE POLICY
# Defines what "necessary only" means in the network-level blocking fallback.
# These domains are blocked via Playwright's request interception when running
# in necessary-only mode, regardless of whether the consent banner was handled.
# ─────────────────────────────────────────────────────────────────────────────

# Third-party domains whose requests are aborted in necessary-only mode.
# This list covers the most common analytics, advertising, and tracking vendors.
# Extend it with any domain patterns specific to your URL database.
THIRD_PARTY_BLOCK_DOMAINS = [
    # Google Analytics / Tag Manager
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    # Meta / Facebook
    "facebook.net",
    "facebook.com/tr",
    "connect.facebook.net",
    # Advertising networks
    "doubleclick.net",
    "googlesyndication.com",
    "adnxs.com",
    "scorecardresearch.com",
    # Session recording / heatmaps
    "hotjar.com",
    "fullstory.com",
    "mouseflow.com",
    # Marketing automation
    "marketo.net",
    "pardot.com",
    "hubspot.com",
    # A/B testing
    "optimizely.com",
    "abtasty.com",
    "vwo.com",
]

# ─────────────────────────────────────────────────────────────────────────────
# METRICS COLLECTION FLAGS
# Toggle individual collection steps on/off. Useful for faster debug runs
# where you don't need screenshots or the full cookie inventory.
# ─────────────────────────────────────────────────────────────────────────────

COLLECT_SCREENSHOTS   = False   # Full-page screenshots for visual diff
COLLECT_CONSOLE_ERRORS = True  # JS console errors and warnings
COLLECT_COOKIE_INVENTORY = True  # Full list of cookies set after load
COLLECT_DOM_SIGNALS   = True   # Check for presence of key DOM elements

# ─────────────────────────────────────────────────────────────────────────────
# DOM SIGNAL CHECKS
# A list of named selectors to probe after page load. If the element is
# present in all-cookies mode but absent in necessary-only mode, it's flagged
# as a functionality regression. Add selectors specific to your site types.
# ─────────────────────────────────────────────────────────────────────────────

DOM_SIGNAL_CHECKS = [
    {"name": "video_embed",      "selector": "iframe[src*='youtube'], iframe[src*='vimeo'], video"},
    {"name": "chat_widget",      "selector": "#intercom-container, .drift-widget, [id*='hubspot-messages']"},
    {"name": "social_buttons",   "selector": ".fb-like, .twitter-share-button, [class*='share-button']"},
    {"name": "map_embed",        "selector": "iframe[src*='google.com/maps'], iframe[src*='maps.google']"},
    {"name": "analytics_pixel",  "selector": "img[src*='facebook.com/tr'], img[src*='google-analytics']"},
    {"name": "ab_test_variant",  "selector": "[data-experiment], [class*='optimizely'], [class*='vwo-']"},
]

# ─────────────────────────────────────────────────────────────────────────────
# IMPACT SCORE WEIGHTS
# Used in diff.py to compute the composite impact score (0.0–1.0).
# Weights must sum to 1.0.
# ─────────────────────────────────────────────────────────────────────────────

IMPACT_WEIGHTS = {
    "load_time_delta":    0.25,
    "request_overhead":   0.20,
    "console_errors":     0.25,
    "missing_elements":   0.20,
    "visual_diff":        0.10,
}

# ─────────────────────────────────────────────────────────────────────────────
# ANALYTICS OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

# How many sites to include in the "top impacted" table.
TOP_N_SITES = 20

# Chart image DPI. 150 is crisp enough for presentations without being huge.
CHART_DPI = 150

# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def ensure_directories():
    """
    Create output directories if they don't already exist.
    Called once at startup by runner.py before any workers launch.
    """
    for path in [SCREENSHOTS_DIR, REPORTS_DIR]:
        os.makedirs(path, exist_ok=True)
