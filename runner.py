"""
runner.py
─────────────────────────────────────────────────────────────────────────────
Entry point for the pipeline. Run this script to start testing URLs.

Usage:
    python runner.py                        # Standard run
    python runner.py --workers 4            # Limit concurrency
    python runner.py --debug                # Single worker, headed browser
    python runner.py --url https://bbc.com  # Test one URL and print results
    python runner.py --load urls.csv        # Load a CSV before running
    python runner.py --stats                # Print queue stats and exit

This script:
  1. Initialises the database and output directories.
  2. Pulls URL batches from the database.
  3. Runs workers in parallel using asyncio + a semaphore for backpressure.
  4. Logs progress and writes a summary on completion.
─────────────────────────────────────────────────────────────────────────────
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import timedelta

import config
import database
from worker import test_site

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# Configures both a human-readable console handler and a file handler.
# The file handler appends across runs, giving a full audit trail.
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(debug: bool = False):
    """
    Configure root logger with console + file handlers.

    In debug mode:
      - Log level drops to DEBUG (verbose Playwright internals visible).
      - Console output is more detailed.

    In normal mode:
      - Console shows INFO and above (progress, warnings, errors).
      - File captures DEBUG and above for post-run diagnosis.
    """
    log_level = logging.DEBUG if debug else logging.INFO
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)   # Capture everything at root, filter per handler

    # Console handler — INFO in production, DEBUG in debug mode
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root.addHandler(console)

    # File handler — always DEBUG so we have full detail for post-mortems
    file_handler = logging.FileHandler(config.LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root.addHandler(file_handler)

    # Quiet down Playwright's own very verbose internal logging
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# WORKER WRAPPER
# Wraps test_site() with the semaphore that controls concurrency.
# ─────────────────────────────────────────────────────────────────────────────

async def run_worker_with_semaphore(
    sem: asyncio.Semaphore,
    domain_last_time: dict,
    domain_lock: asyncio.Lock,
    url: str,
    url_id: int,
    results: dict,
):
    """
    Acquire the semaphore, run the test, then release.

    The semaphore limits how many workers run simultaneously. When all
    MAX_WORKERS slots are occupied, any new coroutine awaiting `async with sem`
    blocks here until a slot frees up. This provides automatic backpressure —
    no URL is lost, it just waits its turn.

    `results` is a shared dict we mutate to track success/failure counts
    across the batch for the progress log.
    """
    from urllib.parse import urlparse
    domain = urlparse(url).hostname or ""

    async with domain_lock:
        last_time = domain_last_time.get(domain, 0)
        now = time.monotonic()
        if now - last_time < config.SAME_DOMAIN_DELAY_S:
            await asyncio.sleep(config.SAME_DOMAIN_DELAY_S - (now - last_time))
        domain_last_time[domain] = time.monotonic()

    async with sem:
        success = await test_site(url, url_id)
        if success:
            results["done"] += 1
        else:
            results["failed"] += 1


# ─────────────────────────────────────────────────────────────────────────────
# BATCH LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def run_pipeline(max_workers: int):
    """
    Main async loop: pull batches from DB, run workers, repeat until empty.

    Design choices:
      - asyncio.gather() runs all tasks in a batch concurrently.
        return_exceptions=True means one crashing task doesn't abort the others.
      - The semaphore inside each task limits actual concurrent browser instances.
      - We track start time to report total wall-clock duration at the end.
    """
    start_time = time.monotonic()
    total_done = 0
    total_failed = 0
    batch_number = 0

    # The semaphore is shared across all tasks in all batches.
    # We create it once here so it persists across batch iterations.
    sem = asyncio.Semaphore(max_workers)

    # Domain throttling: track last access time per domain
    domain_last_time = {}
    domain_lock = asyncio.Lock()

    logger.info("Pipeline starting | workers=%d batch_size=%d", max_workers, config.BATCH_SIZE)

    while True:
        # ── Pull next batch ───────────────────────────────────────────────────
        batch = database.get_next_batch(config.BATCH_SIZE)
        if not batch:
            logger.info("URL queue is empty — pipeline complete.")
            break

        batch_number += 1
        batch_results = {"done": 0, "failed": 0}

        logger.info(
            "Batch %d | %d URLs | queue stats: %s",
            batch_number, len(batch), database.get_queue_stats()
        )

        # ── Dispatch all URLs in this batch concurrently ──────────────────────
        # Each coroutine is created immediately but only runs when it can
        # acquire the semaphore. This means we can have BATCH_SIZE coroutines
        # "in flight" but only MAX_WORKERS actually using a browser at once.
        tasks = [
            run_worker_with_semaphore(sem, domain_last_time, domain_lock, row["url"], row["id"], batch_results)
            for row in batch
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # ── Log batch summary ─────────────────────────────────────────────────
        total_done   += batch_results["done"]
        total_failed += batch_results["failed"]
        elapsed = timedelta(seconds=int(time.monotonic() - start_time))

        logger.info(
            "Batch %d complete | done=%d failed=%d | total done=%d failed=%d | elapsed=%s",
            batch_number,
            batch_results["done"], batch_results["failed"],
            total_done, total_failed,
            elapsed,
        )

    # ── Final summary ─────────────────────────────────────────────────────────
    elapsed = timedelta(seconds=int(time.monotonic() - start_time))
    final_stats = database.get_queue_stats()
    logger.info(
        "Pipeline finished in %s | done=%d failed=%d timeout=%d | DB stats: %s",
        elapsed, total_done, total_failed,
        final_stats.get("timeout", 0), final_stats,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE-URL DEBUG MODE
# Runs one URL and prints the result dict to stdout. No DB write.
# Useful for testing a new site or debugging a consent handler failure.
# ─────────────────────────────────────────────────────────────────────────────

async def run_single_url(url: str):
    """
    Test a single URL and print the results inline.

    Inserts the URL into the database with a temporary ID, runs the test,
    and prints both metric dicts and the diff to stdout.
    Used with the --url CLI flag for development/debugging.
    """
    logger.info("Single-URL mode: %s", url)

    # Insert if not already present
    database.load_urls_from_list([url])

    # Find the ID
    with database.get_conn() as conn:
        row = conn.execute("SELECT id FROM urls WHERE url = ?", (url,)).fetchone()
    if not row:
        logger.error("Failed to find URL in database after insert.")
        return

    url_id = row["id"]
    success = await test_site(url, url_id)

    if success:
        # Fetch and pretty-print the results
        import json
        result = database.get_results_by_url(url_id)
        diffs = {d["url_id"]: d for d in database.get_all_diffs()}
        diff_row = diffs.get(url_id, {})

        print("\n" + "="*60)
        print("ALL COOKIES")
        print("="*60)
        print(json.dumps(result["all"], indent=2, default=str))
        print("\n" + "="*60)
        print("NECESSARY ONLY")
        print("="*60)
        print(json.dumps(result["necessary"], indent=2, default=str))
        print("\n" + "="*60)
        print("DIFF")
        print("="*60)
        print(json.dumps(diff_row, indent=2, default=str))
    else:
        print("Test failed — check the log for details.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI ARGUMENT PARSING
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    """
    Define and parse command-line arguments.

    All arguments are optional — running with no arguments starts a standard
    pipeline run with settings from config.py.
    """
    parser = argparse.ArgumentParser(
        description="Cookie survey pipeline — test hundreds of sites for cookie impact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python runner.py                         # Standard run, settings from config.py
  python runner.py --load urls.csv         # Load URLs then run
  python runner.py --workers 4             # Limit to 4 concurrent browsers
  python runner.py --url https://bbc.co.uk # Debug a single URL
  python runner.py --stats                 # Show queue stats and exit
  python runner.py --debug                 # Headed browser, 1 worker, verbose logs
        """
    )
    parser.add_argument(
        "--load",
        metavar="CSV_PATH",
        help="Load URLs from a CSV file before running. CSV must have a 'url' column.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=config.MAX_WORKERS,
        help=f"Number of concurrent browser instances (default: {config.MAX_WORKERS}).",
    )
    parser.add_argument(
        "--url",
        metavar="URL",
        help="Test a single URL and print results. Does not run the full pipeline.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print queue statistics and exit.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Debug mode: headed browser, 1 worker, verbose logging.",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()

    # Apply debug overrides before anything else
    if args.debug:
        config.HEADLESS = False
        args.workers = 1

    setup_logging(debug=args.debug)

    # Ensure output directories and DB tables exist
    config.ensure_directories()
    database.init_db()

    # ── --load: Import URLs from CSV ──────────────────────────────────────────
    if args.load:
        count = database.load_urls_from_csv(args.load)
        logger.info("Loaded %d URLs from %s", count, args.load)

    # ── --stats: Print queue status and exit ──────────────────────────────────
    if args.stats:
        stats = database.get_queue_stats()
        print("\nQueue statistics:")
        for status, count in sorted(stats.items()):
            print(f"  {status:<12} {count:>6}")
        sys.exit(0)

    # ── --url: Single-URL debug mode ──────────────────────────────────────────
    if args.url:
        asyncio.run(run_single_url(args.url))
        sys.exit(0)

    # ── Standard pipeline run ─────────────────────────────────────────────────
    try:
        asyncio.run(run_pipeline(max_workers=args.workers))
    except KeyboardInterrupt:
        logger.info("Interrupted by user — run can be resumed with the same command.")
        sys.exit(0)
