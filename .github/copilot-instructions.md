# Copilot instructions for cookie-scraper

This file helps future Copilot sessions (and other assistant agents) understand how to build, test, run, and modify this repository.

---

## Build / install / test / lint (commands)

Python environment
- Install deps: pip install -r requirements.txt
- Install Playwright browsers required by the Python code: playwright install chrome
- Run the main pipeline: python runner.py
- Run single-URL debug: python runner.py --url https://example.com
- Load a CSV then run: python runner.py --load urls.csv
- Show queue stats: python runner.py --stats
- Run with headed browser / debug: python runner.py --debug

Node / Playwright test harness
- Install JS deps: npm install
- Run Playwright tests (full suite): npx playwright test
- Run a single Playwright test file: npx playwright test tests/example.spec.js
- Run a single test by title: npx playwright test -g "test name"

Other useful commands
- Regenerate CMP rules (writes lib/autoconsent-rules.json): node generateRules.js

Notes
- package.json currently contains devDependencies for @playwright/test but no npm scripts. Use npx to run Playwright commands.
- There is no dedicated lint script in this repo.

---

## High-level architecture (big picture)

- runner.py: entrypoint and batch orchestration. Pulls URL batches from the DB, spawns concurrent workers (asyncio + semaphore), and writes run summaries.
- worker.py: per-URL coordinator. Launches a single browser instance, runs two isolated contexts ("all" and "necessary" cookie modes), collects metrics, computes diffs, and persists results.
- browser_manager.py: Playwright browser/context lifecycle, request & console listeners, network-blocking rules for "necessary" mode, navigation helpers, and teardown logic.
- consent_handler.py: CMP detection and interaction logic (uses generated rules where applicable). It drives the UI interactions for accept/reject flows.
- collector.py: collects performance timings, network stats, console errors, DOM signals, cookie inventory, and screenshots. Runs collectors concurrently where safe.
- diff.py: computes impact score and other deltas between the two modes.
- database.py: SQLite-backed persistence for URLs, results, diffs, retry counts, and queue state. Configured by config.DB_PATH (results.db).
- analytics.py / reports/: post-processing and chart generation from DB results.
- generateRules.js + lib/autoconsent-rules.json: Node helper that exports DuckDuckGo autoconsent rules into a JSON file consumed by the Python consent handler.

---

## Key repository conventions and patterns

- Cookie modes: tests always run in two modes named exactly "all" and "necessary". Code and DB rows use these strings; do not rename without updating worker logic and DB consumers.

- Context isolation: browser_manager creates one Browser and multiple BrowserContext instances. Each context represents an independent cookie jar and storage. The worker shares a browser between its two contexts to reduce launch overhead.

- Request/console logging: listeners attach at the context and page level and append to mutable lists (request_log, console_log) which are passed into collector.collect_all. These lists are mutated in-place by event handlers; collectors read them after navigation.

- Network blocking: necessary-only mode uses both UI-based rejection and route interception (config.THIRD_PARTY_BLOCK_DOMAINS). Adjust that list in config.py for project-specific vendors.

- Config-driven toggles: many behaviours are controlled by flags in config.py (COLLECT_SCREENSHOTS, COLLECT_COOKIE_INVENTORY, COLLECT_DOM_SIGNALS, etc.). For faster debug runs, toggle these off.

- Screenshots naming: screenshots/<url_id>_<mode>.png (mode is "all" or "necessary"). Keep this naming when consuming screenshots in analytics.

- Database and retries: database.init_db creates tables in results.db. RETRY_LIMIT (config.py) controls how many automatic retries occur before marking a URL permanently failed.

- Timeouts and navigation: PAGE_TIMEOUT_MS and NETWORK_IDLE_TIMEOUT_MS live in config.py. Worker code tries HTTP then HTTPS automatically if initial navigation fails.

- Logging: runner.py sets both console and file logging. run.log is appended across runs; consult it for post-mortem traces.

---

## Files/places an assistant should check first when making changes

- config.py — runtime toggles and paths
- worker.py, browser_manager.py, collector.py — for any change affecting data collection semantics
- consent_handler.py and lib/autoconsent-rules.json — when changing CMP interaction logic
- database.py — schema and writes/reads
- generateRules.js — regenerate if DuckDuckGo autoconsent rules update
- playwright.config.js and tests/ — Playwright test harness

---

## AI assistant / other assistant configs

No CLAUDE.md, .cursorrules, AGENTS.md, .windsurfrules, or AIDER_CONVENTIONS.md were detected in the repository root. If you want the assistant to prefer particular files or have extra rules, add them alongside this file.

---

## Quick troubleshooting hints (explicit project-specific items)

- If sites fail to load in CI or containerized runs, ensure real Chrome is installed (config.BROWSER_CHANNEL = "chrome") or switch to Playwright's bundled Chromium and adjust launch args.
- If you enable screenshots, install pixelmatch/Pillow for visual diffs and ensure SCREENSHOTS_DIR exists (config.ensure_directories() runs at startup).
- To debug consent detection on a single site, use: python runner.py --url https://example.com --debug

---

(Generated by an automated assistant to help future Copilot sessions understand repository structure and commands.)
