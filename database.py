"""
database.py
─────────────────────────────────────────────────────────────────────────────
All database interaction lives here. No SQL appears in any other script.

This module manages three concerns:
  1. Schema — creating the tables if they don't exist.
  2. URL queue — loading URLs in, pulling batches out, updating status.
  3. Results — writing collected metrics and diffs, reading for analytics.

SQLite is used by default. For large runs (10k+ URLs, multiple machines)
swap the connection logic for asyncpg + PostgreSQL without changing any
calling code — the function signatures stay identical.
─────────────────────────────────────────────────────────────────────────────
"""

import sqlite3
import csv
import logging
from datetime import datetime
from contextlib import contextmanager
from config import DB_PATH

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION MANAGEMENT
# Using a context manager means every caller gets a connection that
# auto-commits on success and auto-rolls-back on exception, then closes.
# This prevents connection leaks across hundreds of concurrent workers.
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    """
    Yields a SQLite connection with WAL mode enabled.

    WAL (Write-Ahead Logging) allows multiple readers alongside one writer,
    which is essential when workers are inserting results while the runner
    is reading the URL queue simultaneously.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row   # Rows behave like dicts: row["url"]
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA INITIALISATION
# Called once by runner.py at startup. Safe to call on every run —
# CREATE TABLE IF NOT EXISTS means it's a no-op if tables already exist.
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    """
    Create all tables. Idempotent — safe to call on every startup.

    Table design rationale:
      urls      — one row per URL, tracks queue status and retry count.
      results   — two rows per URL (one per cookie mode), raw metrics.
      diffs     — one row per URL, the computed comparison between modes.
    """
    with get_conn() as conn:
        conn.executescript("""
            -- ── urls ────────────────────────────────────────────────────────
            -- Tracks every URL to be tested and its current processing state.
            -- status values:
            --   pending  → not yet processed
            --   running  → currently being tested (prevents double-processing)
            --   done     → both modes collected, diff written
            --   failed   → raised an unhandled exception
            --   timeout  → page didn't load within PAGE_TIMEOUT_MS
            CREATE TABLE IF NOT EXISTS urls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT NOT NULL UNIQUE,
                status      TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                error_msg   TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            -- ── results ──────────────────────────────────────────────────────
            -- Raw metrics for each (url, cookie_mode) pair.
            -- One row for mode='all', one row for mode='necessary' per URL.
            CREATE TABLE IF NOT EXISTS results (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                url_id              INTEGER NOT NULL REFERENCES urls(id),
                cookie_mode         TEXT NOT NULL CHECK(cookie_mode IN ('all','necessary')),

                -- Performance timings from window.performance
                ttfb_ms             REAL,    -- Time To First Byte
                dom_loaded_ms       REAL,    -- DOMContentLoaded event
                load_time_ms        REAL,    -- Full page load (loadEventEnd)
                lcp_ms              REAL,    -- Largest Contentful Paint (if available)

                -- Network summary from request interception
                request_count       INTEGER,
                bytes_transferred   INTEGER,
                blocked_count       INTEGER, -- Requests aborted in necessary mode
                third_party_domains TEXT,    -- JSON list of unique third-party hosts contacted

                -- Functional signals
                console_error_count INTEGER,
                console_errors      TEXT,    -- JSON list of {level, message} dicts
                dom_signals         TEXT,    -- JSON dict of {check_name: true/false}

                -- Cookie inventory
                cookie_count        INTEGER,
                cookies             TEXT,    -- JSON list of cookie dicts

                -- Screenshot
                screenshot_path     TEXT,

                -- Consent handling outcome
                consent_status      TEXT,    -- "handled" | "not_found" | "failed"
                consent_cmp         TEXT,    -- e.g. "onetrust", "cookiebot", null

                collected_at        TEXT NOT NULL
            );

            -- ── diffs ─────────────────────────────────────────────────────────
            -- Computed comparison between the two result rows for a URL.
            -- Written by diff.py after both result rows exist.
            CREATE TABLE IF NOT EXISTS diffs (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                url_id                  INTEGER NOT NULL UNIQUE REFERENCES urls(id),

                -- Numeric deltas (all_cookies_value MINUS necessary_value)
                -- Positive = metric is higher when all cookies are present
                load_time_delta_ms      REAL,
                request_count_delta     INTEGER,
                bytes_delta             INTEGER,
                console_error_delta     INTEGER,
                cookie_count_delta      INTEGER,

                -- Boolean flags
                has_new_errors          INTEGER, -- 1 if necessary mode has errors all mode doesn't
                missing_elements        TEXT,    -- JSON list of DOM signal names that disappeared

                -- Visual diff (0.0 = identical, 1.0 = completely different)
                visual_diff_score       REAL,

                -- Composite impact score (0.0–1.0)
                impact_score            REAL,

                -- Consent metadata
                consent_status          TEXT,
                consent_cmp             TEXT,

                diffed_at               TEXT NOT NULL
            );

            -- Index for fast batch-pulling of pending URLs
            CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status);

            -- Index for joining results to diffs in analytics queries
            CREATE INDEX IF NOT EXISTS idx_results_url_id ON results(url_id);
        """)
    logger.info("Database initialised at %s", DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# URL QUEUE — LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_urls_from_csv(filepath: str) -> int:
    """
    Read a CSV file of URLs and insert them into the urls table.

    Expected CSV format — header row required, 'url' column must exist:
        url,label
        https://example.com,Example

    Returns the number of new URLs inserted (duplicates are silently skipped
    via INSERT OR IGNORE, so re-importing the same CSV is safe).
    """
    inserted = 0
    now = datetime.utcnow().isoformat()

    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "url" not in reader.fieldnames:
            raise ValueError("CSV must have a 'url' column header.")

        with get_conn() as conn:
            for row in reader:
                url = row["url"].strip()
                """if not url.startswith("http"):
                    logger.warning("Skipping non-HTTP URL: %s", url)
                    continue"""
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO urls (url, status, created_at, updated_at) VALUES (?, 'pending', ?, ?)",
                        (url, now, now)
                    )
                    if conn.execute("SELECT changes()").fetchone()[0]:
                        inserted += 1
                except Exception as e:
                    logger.error("Failed to insert URL %s: %s", url, e)

    logger.info("Loaded %d new URLs from %s", inserted, filepath)
    return inserted


def load_urls_from_list(urls: list[str]) -> int:
    """
    Insert a Python list of URL strings directly (useful for testing
    or programmatic pipeline setup without a CSV file).
    """
    now = datetime.utcnow().isoformat()
    inserted = 0
    with get_conn() as conn:
        for url in urls:
            conn.execute(
                "INSERT OR IGNORE INTO urls (url, status, created_at, updated_at) VALUES (?, 'pending', ?, ?)",
                (url.strip(), now, now)
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# URL QUEUE — BATCH MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def get_next_batch(batch_size: int) -> list[dict]:
    """
    Atomically pull the next batch of pending URLs and mark them 'running'.

    'Atomic' here means we SELECT and UPDATE in a single transaction so two
    concurrent runner processes can't pick the same URLs. SQLite's WAL mode
    ensures the UPDATE is visible to other connections immediately.

    Returns a list of dicts: [{"id": 1, "url": "https://..."}]
    Returns an empty list when the queue is exhausted.
    """
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        # Pull IDs first (avoids a race between SELECT and UPDATE)
        rows = conn.execute(
            "SELECT id, url FROM urls WHERE status = 'pending' LIMIT ?",
            (batch_size,)
        ).fetchall()

        if not rows:
            return []

        ids = [r["id"] for r in rows]
        # Mark all as 'running' in one statement
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE urls SET status='running', updated_at=? WHERE id IN ({placeholders})",
            [now, *ids]
        )
        return [{"id": r["id"], "url": r["url"]} for r in rows]


def get_queue_stats() -> dict:
    """
    Return a summary of URL queue state. Used by the runner to log progress.
    Returns: {"pending": N, "running": N, "done": N, "failed": N, "timeout": N}
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as count FROM urls GROUP BY status"
        ).fetchall()
    return {row["status"]: row["count"] for row in rows}


# ─────────────────────────────────────────────────────────────────────────────
# URL QUEUE — STATUS UPDATES
# Each function is called by worker.py at the end of a test run.
# ─────────────────────────────────────────────────────────────────────────────

def mark_complete(url_id: int):
    """Mark a URL as successfully processed."""
    _update_status(url_id, "done")

def mark_failed(url_id: int, reason: str = ""):
    """Mark a URL as permanently failed after exhausting retries."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE urls SET status='failed', error_msg=?, updated_at=? WHERE id=?",
            (reason[:500], now, url_id)   # Truncate long stack traces
        )

def mark_timeout(url_id: int):
    """Mark a URL as timed out (page didn't reach networkidle in time)."""
    _update_status(url_id, "timeout")

def increment_retry(url_id: int) -> int:
    """
    Increment retry counter and reset status to 'pending' so the URL
    re-enters the queue. Returns the new retry count.
    """
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE urls SET retry_count = retry_count + 1, status = 'pending', updated_at = ? WHERE id = ?",
            (now, url_id)
        )
        row = conn.execute("SELECT retry_count FROM urls WHERE id = ?", (url_id,)).fetchone()
    return row["retry_count"]

def _update_status(url_id: int, status: str):
    """Internal helper to update status + timestamp."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE urls SET status=?, updated_at=? WHERE id=?",
            (status, now, url_id)
        )


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS — WRITING
# ─────────────────────────────────────────────────────────────────────────────

def write_result(url_id: int, cookie_mode: str, metrics: dict):
    """
    Persist collected metrics for one (url, cookie_mode) pair.

    `metrics` is the dict returned by collector.collect_all(). All list/dict
    values are JSON-serialised before storage since SQLite has no native
    JSON column type (though it does have JSON functions we can query with).
    """
    import json
    now = datetime.utcnow().isoformat()

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO results (
                url_id, cookie_mode,
                ttfb_ms, dom_loaded_ms, load_time_ms, lcp_ms,
                request_count, bytes_transferred, blocked_count, third_party_domains,
                console_error_count, console_errors, dom_signals,
                cookie_count, cookies,
                screenshot_path,
                consent_status, consent_cmp,
                collected_at
            ) VALUES (
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?,
                ?, ?,
                ?
            )
        """, (
            url_id, cookie_mode,
            metrics.get("ttfb_ms"),
            metrics.get("dom_loaded_ms"),
            metrics.get("load_time_ms"),
            metrics.get("lcp_ms"),
            metrics.get("request_count"),
            metrics.get("bytes_transferred"),
            metrics.get("blocked_count", 0),
            json.dumps(metrics.get("third_party_domains", [])),
            metrics.get("console_error_count", 0),
            json.dumps(metrics.get("console_errors", [])),
            json.dumps(metrics.get("dom_signals", {})),
            metrics.get("cookie_count", 0),
            json.dumps(metrics.get("cookies", [])),
            metrics.get("screenshot_path"),
            metrics.get("consent_status"),
            metrics.get("consent_cmp"),
            now
        ))
    logger.debug("Wrote result for url_id=%d mode=%s", url_id, cookie_mode)


def write_diff(url_id: int, diff: dict):
    """
    Persist the computed diff for a URL.
    `diff` is the dict returned by diff.compute_diff().
    Uses INSERT OR REPLACE so re-running diff.py on the same data is safe.
    """
    import json
    now = datetime.utcnow().isoformat()

    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO diffs (
                url_id,
                load_time_delta_ms, request_count_delta, bytes_delta,
                console_error_delta, cookie_count_delta,
                has_new_errors, missing_elements,
                visual_diff_score, impact_score,
                consent_status, consent_cmp,
                diffed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            url_id,
            diff.get("load_time_delta_ms"),
            diff.get("request_count_delta"),
            diff.get("bytes_delta"),
            diff.get("console_error_delta"),
            diff.get("cookie_count_delta"),
            int(diff.get("has_new_errors", False)),
            json.dumps(diff.get("missing_elements", [])),
            diff.get("visual_diff_score"),
            diff.get("impact_score"),
            diff.get("consent_status"),
            diff.get("consent_cmp"),
            now
        ))


def write_batch(url_id: int, metrics_all: dict, metrics_nec: dict, diff_result: dict):
    """
    Persist all results for a URL in a single transaction: both modes, diff, and mark complete.
    """
    import json
    now = datetime.utcnow().isoformat()

    with get_conn() as conn:
        # Write all mode
        conn.execute("""
            INSERT INTO results (
                url_id, cookie_mode,
                ttfb_ms, dom_loaded_ms, load_time_ms, lcp_ms,
                request_count, bytes_transferred, blocked_count, third_party_domains,
                console_error_count, console_errors, dom_signals,
                cookie_count, cookies,
                screenshot_path,
                consent_status, consent_cmp,
                collected_at
            ) VALUES (
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?,
                ?, ?,
                ?
            )
        """, (
            url_id, "all",
            metrics_all.get("ttfb_ms"),
            metrics_all.get("dom_loaded_ms"),
            metrics_all.get("load_time_ms"),
            metrics_all.get("lcp_ms"),
            metrics_all.get("request_count"),
            metrics_all.get("bytes_transferred"),
            metrics_all.get("blocked_count", 0),
            json.dumps(metrics_all.get("third_party_domains", [])),
            metrics_all.get("console_error_count", 0),
            json.dumps(metrics_all.get("console_errors", [])),
            json.dumps(metrics_all.get("dom_signals", {})),
            metrics_all.get("cookie_count", 0),
            json.dumps(metrics_all.get("cookies", [])),
            metrics_all.get("screenshot_path"),
            metrics_all.get("consent_status"),
            metrics_all.get("consent_cmp"),
            now
        ))

        # Write necessary mode
        conn.execute("""
            INSERT INTO results (
                url_id, cookie_mode,
                ttfb_ms, dom_loaded_ms, load_time_ms, lcp_ms,
                request_count, bytes_transferred, blocked_count, third_party_domains,
                console_error_count, console_errors, dom_signals,
                cookie_count, cookies,
                screenshot_path,
                consent_status, consent_cmp,
                collected_at
            ) VALUES (
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?,
                ?, ?,
                ?
            )
        """, (
            url_id, "necessary",
            metrics_nec.get("ttfb_ms"),
            metrics_nec.get("dom_loaded_ms"),
            metrics_nec.get("load_time_ms"),
            metrics_nec.get("lcp_ms"),
            metrics_nec.get("request_count"),
            metrics_nec.get("bytes_transferred"),
            metrics_nec.get("blocked_count", 0),
            json.dumps(metrics_nec.get("third_party_domains", [])),
            metrics_nec.get("console_error_count", 0),
            json.dumps(metrics_nec.get("console_errors", [])),
            json.dumps(metrics_nec.get("dom_signals", {})),
            metrics_nec.get("cookie_count", 0),
            json.dumps(metrics_nec.get("cookies", [])),
            metrics_nec.get("screenshot_path"),
            metrics_nec.get("consent_status"),
            metrics_nec.get("consent_cmp"),
            now
        ))

        # Write diff
        conn.execute("""
            INSERT OR REPLACE INTO diffs (
                url_id,
                load_time_delta_ms, request_count_delta, bytes_delta,
                console_error_delta, cookie_count_delta,
                has_new_errors, missing_elements,
                visual_diff_score, impact_score,
                consent_status, consent_cmp,
                diffed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            url_id,
            diff_result.get("load_time_delta_ms"),
            diff_result.get("request_count_delta"),
            diff_result.get("bytes_delta"),
            diff_result.get("console_error_delta"),
            diff_result.get("cookie_count_delta"),
            int(diff_result.get("has_new_errors", False)),
            json.dumps(diff_result.get("missing_elements", [])),
            diff_result.get("visual_diff_score"),
            diff_result.get("impact_score"),
            diff_result.get("consent_status"),
            diff_result.get("consent_cmp"),
            now
        ))

        # Mark complete
        conn.execute(
            "UPDATE urls SET status=?, updated_at=? WHERE id=?",
            ("complete", now, url_id)
        )

    logger.debug("Batch wrote results for url_id=%d", url_id)


def mark_complete(url_id: int):
    """Mark a URL as completed."""
    _update_status(url_id, "complete")


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS — READING (used by analytics.py)
# ─────────────────────────────────────────────────────────────────────────────

def get_all_diffs() -> list[dict]:
    """
    Return all diff rows joined with their URL for analytics.
    Each dict includes the URL string alongside the diff columns.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT d.*, u.url
            FROM diffs d
            JOIN urls u ON u.id = d.url_id
            ORDER BY d.impact_score DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_results_by_url(url_id: int) -> dict:
    """
    Return both result rows for a URL as a dict keyed by mode.
    Returns: {"all": {...}, "necessary": {...}}
    Raises ValueError if either mode is missing.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM results WHERE url_id = ?", (url_id,)
        ).fetchall()

    result = {}
    for row in rows:
        result[row["cookie_mode"]] = dict(row)

    if "all" not in result or "necessary" not in result:
        raise ValueError(f"Incomplete results for url_id={url_id}")
    return result


def get_summary_stats() -> dict:
    """
    Compute aggregate statistics across all completed URLs.
    Used by analytics.py to populate the headline summary table.
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(DISTINCT url_id)                      AS total_sites,
                AVG(CASE WHEN cookie_mode='all'       THEN load_time_ms END) AS avg_load_all,
                AVG(CASE WHEN cookie_mode='necessary' THEN load_time_ms END) AS avg_load_nec,
                AVG(CASE WHEN cookie_mode='all'       THEN request_count END) AS avg_req_all,
                AVG(CASE WHEN cookie_mode='necessary' THEN request_count END) AS avg_req_nec,
                AVG(CASE WHEN cookie_mode='all'       THEN bytes_transferred END) AS avg_bytes_all,
                AVG(CASE WHEN cookie_mode='necessary' THEN bytes_transferred END) AS avg_bytes_nec
            FROM results
        """).fetchone()
    return dict(row)
