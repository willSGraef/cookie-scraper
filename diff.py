"""
diff.py
─────────────────────────────────────────────────────────────────────────────
Compares the two metric dicts (all-cookies vs necessary-only) for a URL
and produces a clean diff with an overall composite impact score.

The impact score (0.0–1.0) is the single number that summarises how
different the two cookie experiences are. It feeds the "top impacted sites"
table in the analytics report.

This module has no I/O — it takes two dicts and returns a dict.
All persistence is handled by database.write_diff().
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import math
import os
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# NUMERIC DELTAS
# ─────────────────────────────────────────────────────────────────────────────

def compute_numeric_deltas(metrics_all: dict, metrics_nec: dict) -> dict:
    """
    Compute raw numeric differences between the two modes.

    Convention: delta = all_cookies_value - necessary_value
      - Positive delta → metric is higher when all cookies are present.
        e.g. load_time_delta_ms = +500 means the all-cookies page is 500ms slower.
      - Negative delta → metric is lower when all cookies are present.
        This is unusual but possible (e.g. if necessary mode loads extra fallback content).

    All values default to 0 if a field is missing (collector may have
    returned None for a failed sub-collection).
    """

    def _safe_delta(key: str) -> Optional[float]:
        """Return all - necessary for a numeric key, or None if both are missing."""
        val_all = metrics_all.get(key)
        val_nec = metrics_nec.get(key)
        if val_all is None and val_nec is None:
            return None
        return (val_all or 0) - (val_nec or 0)

    return {
        # Performance — positive means all-cookies page is heavier/slower
        "load_time_delta_ms":   _safe_delta("load_time_ms"),
        "ttfb_delta_ms":        _safe_delta("ttfb_ms"),
        "dom_loaded_delta_ms":  _safe_delta("dom_loaded_ms"),

        # Network — positive means more traffic with all cookies
        "request_count_delta":  _safe_delta("request_count"),
        "bytes_delta":          _safe_delta("bytes_transferred"),
        "blocked_count":        metrics_nec.get("blocked_count", 0),  # Absolute, not delta

        # Functional
        "console_error_delta":  _safe_delta("console_error_count"),
        "cookie_count_delta":   _safe_delta("cookie_count"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# BOOLEAN FLAGS
# ─────────────────────────────────────────────────────────────────────────────

def compute_flags(metrics_all: dict, metrics_nec: dict) -> dict:
    """
    Compute boolean/categorical differences that can't be expressed as a delta.

    Key outputs:
      has_new_errors    — True if necessary mode introduces console errors that
                          are absent in all-cookies mode. This is the clearest
                          signal of a functional regression caused by cookie removal.

      missing_elements  — List of DOM signal names that were present in all-cookies
                          mode but absent in necessary-only mode. Each name
                          corresponds to a feature category (e.g. "video_embed").

      consent_status    — The consent handler outcome (same for both modes on a
                          given site, taken from the necessary-only mode run since
                          that's the more complex interaction).
    """
    # ── New errors in necessary mode ─────────────────────────────────────────
    # We compare error message texts between modes. If necessary mode has errors
    # that all-cookies mode doesn't, those errors were likely caused by scripts
    # that expect cookies which are now absent.
    errors_all = {e["message"] for e in (metrics_all.get("console_errors") or [])}
    errors_nec = {e["message"] for e in (metrics_nec.get("console_errors") or [])}
    new_errors = errors_nec - errors_all   # Errors present in necessary but not all
    has_new_errors = len(new_errors) > 0

    # ── Missing DOM elements ──────────────────────────────────────────────────
    # Compare each DOM signal check between modes.
    # A signal is "missing" if it was True (present) in all-cookies mode
    # but False (absent) in necessary-only mode.
    dom_all = metrics_all.get("dom_signals") or {}
    dom_nec = metrics_nec.get("dom_signals") or {}
    missing_elements = [
        name
        for name in dom_all
        if dom_all.get(name) is True and dom_nec.get(name) is False
    ]

    return {
        "has_new_errors":    has_new_errors,
        "new_error_count":   len(new_errors),
        "missing_elements":  missing_elements,
        "consent_status":    metrics_nec.get("consent_status", "unknown"),
        "consent_cmp":       metrics_nec.get("consent_cmp"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# VISUAL DIFF
# Compares screenshots pixel-by-pixel to quantify visual changes.
# ─────────────────────────────────────────────────────────────────────────────

def compute_visual_diff(path_all: Optional[str], path_nec: Optional[str]) -> Optional[float]:
    """
    Compare two full-page screenshots and return a normalised diff score.

    Uses the pixelmatch algorithm (same as the popular JS library of the same
    name) which compares pixels using a perceptual colour difference metric
    (Delta E) rather than raw RGB values. This makes it tolerant of
    anti-aliasing and sub-pixel rendering differences that aren't visually
    meaningful.

    Returns a score between 0.0 (identical) and 1.0 (completely different),
    or None if screenshots aren't available.

    Note: Images are resized to a common height before comparison because
    pages may render at different heights between modes (e.g. if a cookie-gated
    banner takes up vertical space in one mode).
    """
    if not path_all or not path_nec:
        return None
    if not os.path.exists(path_all) or not os.path.exists(path_nec):
        return None

    try:
        from PIL import Image
        from pixelmatch.contrib.PIL import pixelmatch

        img_all = Image.open(path_all).convert("RGBA")
        img_nec = Image.open(path_nec).convert("RGBA")

        # Normalise dimensions — crop to the shorter of the two heights
        # (differences at the bottom are usually just extra whitespace)
        min_height = min(img_all.height, img_nec.height)
        min_width  = min(img_all.width,  img_nec.width)

        img_all = img_all.crop((0, 0, min_width, min_height))
        img_nec = img_nec.crop((0, 0, min_width, min_height))

        # Run pixelmatch — returns count of mismatched pixels
        total_pixels = min_width * min_height
        mismatch_count = pixelmatch(img_all, img_nec, threshold=0.1)

        score = mismatch_count / total_pixels if total_pixels > 0 else 0.0
        return round(score, 4)

    except ImportError:
        logger.debug("pixelmatch or PIL not installed — skipping visual diff")
        return None
    except Exception as e:
        logger.warning("Visual diff failed: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# IMPACT SCORE
# A single composite number (0.0–1.0) summarising overall cookie impact.
# ─────────────────────────────────────────────────────────────────────────────

def compute_impact_score(deltas: dict, flags: dict, visual_diff: Optional[float]) -> float:
    """
    Compute a weighted composite impact score.

    Each component is normalised to 0.0–1.0 before weighting so they're
    on a comparable scale. Weights are defined in config.IMPACT_WEIGHTS.

    Component normalisation:
      load_time_delta  — clamp to [0, 30000ms] range, normalise
      request_overhead — clamp to [0, 1000 requests] range, normalise
      console_errors   — binary: 0 if no new errors, 1 if any
      missing_elements — normalise against total number of DOM checks
      visual_diff      — already 0.0–1.0

    A score of 0.0 means the two modes are identical.
    A score of 1.0 means maximum impact across all dimensions.
    """
    weights = config.IMPACT_WEIGHTS
    components = {}

    # ── Load time delta component ─────────────────────────────────────────────
    # We only count positive deltas (all-cookies being slower).
    # A necessary-only page being slower is unusual and not a "cookie impact".
    load_delta = max(0, deltas.get("load_time_delta_ms") or 0)
    components["load_time_delta"] = min(load_delta / 30_000, 1.0)

    # ── Request overhead component ────────────────────────────────────────────
    req_delta = max(0, deltas.get("request_count_delta") or 0)
    bytes_delta = max(0, deltas.get("bytes_delta") or 0)
    # Combine request count and bytes — normalise each to [0,1] and average
    req_norm   = min(req_delta / 1_000, 1.0)
    bytes_norm = min(bytes_delta / (5 * 1024 * 1024), 1.0)  # 5MB ceiling
    components["request_overhead"] = (req_norm + bytes_norm) / 2

    # ── Console errors component ──────────────────────────────────────────────
    # Binary: any new errors in necessary mode = maximum penalty.
    # We use tanh to give partial credit for many vs few new errors.
    new_error_count = flags.get("new_error_count", 0)
    components["console_errors"] = math.tanh(new_error_count / 2) if new_error_count else 0.0

    # ── Missing DOM elements component ───────────────────────────────────────
    total_checks  = len(config.DOM_SIGNAL_CHECKS)
    missing_count = len(flags.get("missing_elements") or [])
    components["missing_elements"] = missing_count / total_checks if total_checks > 0 else 0.0

    # ── Visual diff component ─────────────────────────────────────────────────
    components["visual_diff"] = visual_diff if visual_diff is not None else 0.0

    # ── Weighted sum ──────────────────────────────────────────────────────────
    score = sum(
        components[key] * weights[key]
        for key in weights
        if key in components
    )

    return round(min(score, 1.0), 4)


# ─────────────────────────────────────────────────────────────────────────────
# MASTER DIFF
# ─────────────────────────────────────────────────────────────────────────────

def compute_diff(metrics_all: dict, metrics_nec: dict) -> dict:
    """
    Compute the full diff between all-cookies and necessary-only metrics.

    This is the only public function most callers need. It orchestrates the
    component functions and assembles a single flat dict ready for
    database.write_diff().

    Returns a dict with all delta, flag, visual, and score fields.
    """
    deltas      = compute_numeric_deltas(metrics_all, metrics_nec)
    flags       = compute_flags(metrics_all, metrics_nec)
    visual_diff = compute_visual_diff(
        metrics_all.get("screenshot_path"),
        metrics_nec.get("screenshot_path"),
    )
    impact_score = compute_impact_score(deltas, flags, visual_diff)

    # Flatten everything into one result dict for the DB writer
    result = {}
    result.update(deltas)
    result.update(flags)
    result["visual_diff_score"] = visual_diff
    result["impact_score"]      = impact_score

    logger.debug(
        "Diff computed | impact=%.3f load_delta=%.0fms errors=%s missing=%s",
        impact_score,
        deltas.get("load_time_delta_ms") or 0,
        flags["has_new_errors"],
        flags["missing_elements"],
    )

    return result
