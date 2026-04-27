"""
analytics.py
─────────────────────────────────────────────────────────────────────────────
Reads from the results database and produces presentation-ready outputs.

Run this script after a pipeline run completes:
    python analytics.py

Outputs written to config.REPORTS_DIR:
  performance_summary.csv   — headline load/request/bytes comparison
  functionality_impact.csv  — which features broke and on how many sites
  top_impacted.csv          — top N sites ranked by impact score
  cmp_coverage.csv          — consent platform detection statistics
  cookie_categories.csv     — breakdown of cookie types set per mode
  load_time_dist.png        — overlapping histogram of load times
  impact_scatter.png        — scatter of site size vs impact score
  functionality_bar.png     — horizontal bar of feature impact rates
  full_report.html          — self-contained HTML report with everything

All charts use matplotlib. For interactive charts in a Jupyter notebook,
swap the savefig() calls for plt.show() and comment out the file outputs.
─────────────────────────────────────────────────────────────────────────────
"""

import base64
import json
import logging
import os
from datetime import datetime

import pandas as pd
import matplotlib
matplotlib.use("Agg")   # Non-interactive backend — no display needed on a server
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

import config
import database

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# Loads raw data from the database once and transforms it into DataFrames
# that the reporting functions share.
# ─────────────────────────────────────────────────────────────────────────────

def load_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Load all results and diffs from the database into pandas DataFrames.

    Returns three DataFrames:
      df_results — one row per (url, cookie_mode), raw metrics
      df_diffs   — one row per url, computed differences + impact score
      df_urls    — one row per url with its status

    JSON columns (console_errors, dom_signals, cookies, third_party_domains)
    are parsed back from strings into Python objects.
    """

    # ── Raw results ───────────────────────────────────────────────────────────
    with database.get_conn() as conn:
        results_rows = conn.execute("""
            SELECT r.*, u.url
            FROM results r
            JOIN urls u ON u.id = r.url_id
        """).fetchall()

        diffs_rows = conn.execute("""
            SELECT d.*, u.url
            FROM diffs d
            JOIN urls u ON u.id = d.url_id
        """).fetchall()

        urls_rows = conn.execute("SELECT * FROM urls").fetchall()

    # Convert to DataFrames
    df_results = pd.DataFrame([dict(r) for r in results_rows])
    df_diffs   = pd.DataFrame([dict(r) for r in diffs_rows])
    df_urls    = pd.DataFrame([dict(r) for r in urls_rows])

    # ── Parse JSON columns ────────────────────────────────────────────────────
    # These were stored as JSON strings. Parse them back so we can work
    # with the underlying Python structures in reporting functions.
    json_columns = ["console_errors", "dom_signals", "cookies", "third_party_domains"]
    for col in json_columns:
        if col in df_results.columns:
            df_results[col] = df_results[col].apply(
                lambda x: json.loads(x) if isinstance(x, str) else (x or [])
            )

    if "missing_elements" in df_diffs.columns:
        df_diffs["missing_elements"] = df_diffs["missing_elements"].apply(
            lambda x: json.loads(x) if isinstance(x, str) else (x or [])
        )

    logger.info(
        "Loaded %d result rows, %d diff rows, %d url rows",
        len(df_results), len(df_diffs), len(df_urls)
    )
    return df_results, df_diffs, df_urls


# ─────────────────────────────────────────────────────────────────────────────
# TABLE 1: OVERALL PERFORMANCE SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def table_overall_performance(df_results: pd.DataFrame) -> pd.DataFrame:
    """
    Build the headline performance comparison table.

    Computes mean and median for each key metric, split by cookie mode,
    then calculates the absolute and percentage delta.

    Output columns:
      metric | mean_all | mean_necessary | mean_delta | mean_delta_pct
             | median_all | median_necessary | median_delta

    Saved to: performance_summary.csv
    """
    metrics = [
        ("dom_loaded_ms",   "DOM Content Loaded (ms)"),
        ("ttfb_ms",         "TTFB (ms)"),
        ("load_time_ms",    "Load time (ms)"),
        ("request_count",   "HTTP requests"),
        ("bytes_transferred", "Bytes transferred"),
        ("cookie_count",    "Cookies set"),
        ("console_error_count", "Console errors"),
    ]

    rows = []
    for col, label in metrics:
        if col not in df_results.columns:
            continue

        group = df_results.groupby("cookie_mode")[col]
        mean_all = group.mean().get("all", 0)
        mean_nec = group.mean().get("necessary", 0)
        med_all  = group.median().get("all", 0)
        med_nec  = group.median().get("necessary", 0)

        mean_delta     = mean_all - mean_nec
        mean_delta_pct = (mean_delta / mean_nec * 100) if mean_nec else 0

        rows.append({
            "Metric":           label,
            "Mean (all)":       round(mean_all, 1),
            "Mean (necessary)": round(mean_nec, 1),
            "Mean delta":       round(mean_delta, 1),
            "Delta %":          round(mean_delta_pct, 1),
            "Median (all)":     round(med_all, 1),
            "Median (necessary)": round(med_nec, 1),
        })

    df = pd.DataFrame(rows)
    _save_csv(df, "performance_summary.csv")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# TABLE 2: FUNCTIONALITY IMPACT
# ─────────────────────────────────────────────────────────────────────────────

def table_functionality_impact(df_diffs: pd.DataFrame) -> pd.DataFrame:
    """
    Count how many sites lost each DOM-signal feature in necessary mode.

    Uses the missing_elements column from diffs, which lists DOM signal
    names that were present in all-cookies mode but absent in necessary-only.

    Output columns:
      Feature | Sites affected | % of total | Also had console errors

    Saved to: functionality_impact.csv
    """
    total_sites = len(df_diffs)
    if total_sites == 0:
        return pd.DataFrame()

    # Count how many times each feature name appears in missing_elements lists
    feature_counts = {}
    for missing_list in df_diffs["missing_elements"]:
        for feature in (missing_list or []):
            feature_counts[feature] = feature_counts.get(feature, 0) + 1

    rows = []
    for feature, count in sorted(feature_counts.items(), key=lambda x: -x[1]):
        rows.append({
            "Feature":        feature.replace("_", " ").title(),
            "Sites affected": count,
            "% of sites":     round(count / total_sites * 100, 1),
        })

    # Add a row for sites that introduced new console errors
    new_error_sites = df_diffs["has_new_errors"].sum()
    rows.append({
        "Feature":        "Console errors introduced",
        "Sites affected": int(new_error_sites),
        "% of sites":     round(new_error_sites / total_sites * 100, 1),
    })

    df = pd.DataFrame(rows).sort_values("Sites affected", ascending=False)
    _save_csv(df, "functionality_impact.csv")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# TABLE 3: TOP IMPACTED SITES
# ─────────────────────────────────────────────────────────────────────────────

def table_top_impacted(df_diffs: pd.DataFrame, n: int = None) -> pd.DataFrame:
    """
    Return the top N sites sorted by impact score descending.

    This is the "most interesting" table for a presentation — it shows
    the sites where cookie consent made the biggest measurable difference.

    Output columns:
      URL | Impact score | DOM load delta (ms) | Request delta | Errors introduced | Missing features

    Saved to: top_impacted.csv
    """
    n = n or config.TOP_N_SITES

    df = df_diffs.copy()
    df = df.sort_values("dom_loaded_delta_ms", ascending=False).head(n)

    output = df[[
        "url", "dom_loaded_delta_ms",
        "request_count_delta", "cookie_count_delta", "missing_elements",
        "consent_status", "consent_cmp",
    ]].copy()

    output.columns = [
        "URL", "DOM load delta (ms)",
        "Request delta", "Console Errors", "Missing features",
        "Consent status", "CMP",
    ]

    # Pretty-print the missing_elements list
    output["Missing features"] = output["Missing features"].apply(
        lambda x: ", ".join(x) if x else "none"
    )

    _save_csv(output, "top_impacted.csv")
    return output


# ─────────────────────────────────────────────────────────────────────────────
# TABLE 4: CMP COVERAGE
# ─────────────────────────────────────────────────────────────────────────────

def table_cmp_coverage(df_diffs: pd.DataFrame) -> pd.DataFrame:
    """
    Summarise consent platform detection and handling outcomes.

    Shows:
      - Which CMPs were detected and how often.
      - What proportion were handled vs not_found vs failed.
      - Sites with no detected CMP (may still have a custom banner).

    Saved to: cmp_coverage.csv
    """
    total = len(df_diffs)

    # Group by CMP name (null → "None detected")
    df_diffs = df_diffs.copy()
    df_diffs["consent_cmp"] = df_diffs["consent_cmp"].fillna("None detected")

    cmp_group = df_diffs.groupby("consent_cmp")["consent_status"].value_counts().unstack(fill_value=0)

    # Ensure all status columns exist even if some have zero counts
    for col in ["handled", "not_found", "failed"]:
        if col not in cmp_group.columns:
            cmp_group[col] = 0

    cmp_group["Total"] = cmp_group.sum(axis=1)
    #cmp_group["% handled"] = (cmp_group.get("handled", 0) / cmp_group["Total"] * 100).round(1)
    cmp_group = cmp_group.sort_values("Total", ascending=False)

    cmp_group.index.name = "CMP Platform"
    #cmp_group = cmp_group.drop("consent_status", axis=1)
    _save_csv(cmp_group.reset_index(), "cmp_coverage.csv")
    return cmp_group.reset_index()


# ─────────────────────────────────────────────────────────────────────────────
# TABLE 5: COOKIE CATEGORY BREAKDOWN
# ─────────────────────────────────────────────────────────────────────────────

def table_cookie_categories(df_results: pd.DataFrame) -> pd.DataFrame:
    """
    Count cookies by risk tier across both modes.

    Uses the risk_tier field produced by collector.collect_cookies().
    Shows which tiers are most prevalent and how they differ between modes.

    Saved to: cookie_categories.csv
    """
    rows = []
    for _, row in df_results.iterrows():
        mode    = row.get("cookie_mode")
        cookies = row.get("cookies") or []
        for cookie in cookies:
            # Prefer the new risk_tier field, fall back to legacy category if present
            rows.append({
                "cookie_mode": mode,
                "risk_tier":    cookie.get("risk_tier", cookie.get("category", "unknown")),
            })

    if not rows:
        return pd.DataFrame()

    df_cookies = pd.DataFrame(rows)
    pivot = df_cookies.groupby(["risk_tier", "cookie_mode"]).size().unstack(fill_value=0)

    # Ensure both mode columns exist
    for mode in ["all", "necessary"]:
        if mode not in pivot.columns:
            pivot[mode] = 0

    pivot["delta"] = pivot["all"] - pivot["necessary"]
    pivot = pivot.sort_values("all", ascending=False)
    pivot.index.name = "Cookie risk tier"

    _save_csv(pivot.reset_index(), "cookie_categories.csv")
    return pivot.reset_index()


# ─────────────────────────────────────────────────────────────────────────────
# CHART 1: LOAD TIME DISTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

def chart_load_time_distribution(df_results: pd.DataFrame):
    """
    Overlapping histograms comparing load time distributions between modes.

    Shows not just the average difference but the shape of the distribution —
    are all sites slightly slower with all cookies, or is the slowdown
    concentrated in a subset of sites?

    Saved to: load_time_dist.png
    """
    fig, ax = plt.subplots(figsize=(10, 5))

    for mode, color, label in [
        ("all",       "#E8593C", "All cookies"),
        ("necessary", "#3B8BD4", "Necessary only"),
    ]:
        data = df_results[df_results["cookie_mode"] == mode]["load_time_ms"].dropna()
        # Cap at 15s for readability — extreme outliers skew the axis
        data = data[data <= 15_000]
        ax.hist(
            data, bins=50, alpha=0.6, color=color, label=label,
            edgecolor="none"
        )

    ax.set_xlabel("Page load time (ms)", fontsize=11)
    ax.set_ylabel("Number of sites", fontsize=11)
    ax.set_title("Load time distribution: all cookies vs necessary only", fontsize=12, pad=12)
    ax.legend(fontsize=10)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}ms"))

    _save_chart(fig, "load_time_dist.png")


# ─────────────────────────────────────────────────────────────────────────────
# CHART 2: IMPACT SCORE SCATTER
# ─────────────────────────────────────────────────────────────────────────────

def chart_impact_score_scatter(df_diffs: pd.DataFrame, df_results: pd.DataFrame):
    """
    Scatter plot: page size (bytes) vs impact score, coloured by consent status.

    Helps answer "do larger sites suffer more from cookie restrictions?"
    Also surfaces whether sites where consent wasn't handled cluster at
    different impact scores.

    Saved to: impact_scatter.png
    """
    # Get bytes for all-cookies mode to represent "full site size"
    size_df = df_results[df_results["cookie_mode"] == "all"][["url_id", "bytes_transferred"]]
    merged  = df_diffs.merge(size_df, on="url_id", how="left")

    status_colors = {
        "handled":   "#3B8BD4",
        "not_found": "#E8593C",
        "failed":    "#EF9F27",
        "unknown":   "#888780",
    }

    fig, ax = plt.subplots(figsize=(10, 6))

    for status, color in status_colors.items():
        subset = merged[merged["consent_status"] == status]
        if subset.empty:
            continue
        ax.scatter(
            subset["bytes_transferred"] / 1_048_576,   # Convert to MB
            subset["impact_score"],
            c=color, alpha=0.6, s=30, label=f"Consent: {status}", edgecolors="none"
        )

    ax.set_xlabel("Page size (MB, all-cookies mode)", fontsize=11)
    ax.set_ylabel("Impact score (0 = no impact, 1 = maximum)", fontsize=11)
    ax.set_title("Site size vs cookie impact score", fontsize=12, pad=12)
    ax.legend(fontsize=9)
    ax.set_xlim(0, 200)
    ax.set_ylim(0, 1.05)

    _save_chart(fig, "impact_scatter.png")


# ─────────────────────────────────────────────────────────────────────────────
# CHART 3: FUNCTIONALITY IMPACT BAR
# ─────────────────────────────────────────────────────────────────────────────

def chart_functionality_bar(df_impact: pd.DataFrame):
    """
    Horizontal bar chart showing % of sites affected per feature type.

    Best chart for a slide — immediately shows which feature categories
    are most at risk when cookies are restricted.

    Saved to: functionality_bar.png
    """
    if df_impact.empty:
        logger.warning("Functionality impact table is empty — skipping bar chart.")
        return

    df = df_impact.sort_values("% of sites", ascending=True)

    fig, ax = plt.subplots(figsize=(9, max(4, len(df) * 0.5)))

    bars = ax.barh(
        df["Feature"],
        df["% of sites"],
        color="#3B8BD4",
        edgecolor="none",
        height=0.6,
    )

    # Add value labels on the bars
    for bar, val in zip(bars, df["% of sites"]):
        ax.text(
            bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
            f"{val:.1f}%", va="center", fontsize=9
        )

    ax.set_xlabel("% of sites affected", fontsize=11)
    ax.set_title("Features affected when reducing to necessary cookies", fontsize=12, pad=12)
    ax.set_xlim(0, 105)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    _save_chart(fig, "functionality_bar.png")


# ─────────────────────────────────────────────────────────────────────────────
# HTML REPORT
# ─────────────────────────────────────────────────────────────────────────────

def export_html_report(tables: dict, chart_files: list):
    """
    Assemble all tables and charts into a single self-contained HTML file.

    Images are embedded as base64 data URIs so the report file is
    fully portable — no external dependencies.

    Saved to: full_report.html
    """
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Build HTML sections for each table
    table_sections = ""
    for title, df in tables.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        # Convert DataFrame to HTML table with Bootstrap-style classes
        html_table = df.to_html(
            index=False, border=0, classes="data-table",
            float_format=lambda x: f"{x:.1f}"
        )
        table_sections += f"""
        <section>
            <h2>{title}</h2>
            {html_table}
        </section>
        """

    # Build HTML sections for each chart (embedded as base64)
    chart_sections = ""
    for chart_path in chart_files:
        if not os.path.exists(chart_path):
            continue
        chart_title = os.path.basename(chart_path).replace("_", " ").replace(".png", "").title()
        with open(chart_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        chart_sections += f"""
        <section>
            <h2>{chart_title}</h2>
            <img src="data:image/png;base64,{b64}" alt="{chart_title}" style="max-width:100%">
        </section>
        """

    # Minimal, clean CSS for readability
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Cookie Survey Report</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px; color: #222; }}
  h1   {{ font-size: 1.6rem; border-bottom: 2px solid #ddd; padding-bottom: 8px; }}
  h2   {{ font-size: 1.2rem; margin-top: 40px; color: #333; }}
  .data-table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  .data-table th {{ background: #f0f0f0; border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
  .data-table td {{ border: 1px solid #eee; padding: 5px 10px; }}
  .data-table tr:nth-child(even) {{ background: #fafafa; }}
  section  {{ margin-bottom: 48px; }}
  .meta    {{ color: #888; font-size: 0.8rem; margin-bottom: 32px; }}
</style>
</head>
<body>
<h1>Cookie Survey Report</h1>
<p class="meta">Generated: {generated_at}</p>
{table_sections}
{chart_sections}
</body>
</html>"""

    report_path = os.path.join(config.REPORTS_DIR, "full_report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("HTML report written to %s", report_path)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _save_csv(df: pd.DataFrame, filename: str):
    """Save a DataFrame as CSV to the reports directory."""
    path = os.path.join(config.REPORTS_DIR, filename)
    df.to_csv(path, index=False, encoding="utf-8")
    logger.info("Saved table: %s", path)


def _save_chart(fig: plt.Figure, filename: str):
    """Save a matplotlib figure to the reports directory."""
    path = os.path.join(config.REPORTS_DIR, filename)
    fig.tight_layout()
    fig.savefig(path, dpi=config.CHART_DPI, bbox_inches="tight")
    plt.close(fig)
    logger.info("Saved chart: %s", path)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def run_all():
    """
    Generate all tables and charts from the current database state.
    Call this after a pipeline run completes.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    config.ensure_directories()

    logger.info("Loading data from database...")
    df_results, df_diffs, df_urls = load_data()

    if df_results.empty:
        logger.warning("No results found in database. Run the pipeline first.")
        return

    # ── Generate tables ───────────────────────────────────────────────────────
    logger.info("Generating tables...")
    t_performance  = table_overall_performance(df_results)
    t_functionality = table_functionality_impact(df_diffs)
    t_top          = table_top_impacted(df_diffs)
    t_cmp          = table_cmp_coverage(df_diffs)
    t_categories   = table_cookie_categories(df_results)

    # ── Generate charts ───────────────────────────────────────────────────────
    logger.info("Generating charts...")
    chart_load_time_distribution(df_results)
    chart_impact_score_scatter(df_diffs, df_results)
    chart_functionality_bar(t_functionality)

    # ── Generate HTML report ──────────────────────────────────────────────────
    logger.info("Generating HTML report...")
    tables = {
        "Overall performance":     t_performance,
        "Functionality impact":    t_functionality,
        "Top impacted sites":      t_top,
        "Consent platform coverage": t_cmp,
        "Cookie categories":       t_categories,
    }
    chart_files = [
        os.path.join(config.REPORTS_DIR, f)
        for f in ["load_time_dist.png", "impact_scatter.png", "functionality_bar.png"]
    ]
    export_html_report(tables, chart_files)

    # ── Print headline stats to console ───────────────────────────────────────
    summary = database.get_summary_stats()
    print(f"""
─────────────────────────────────────────────
 Cookie Survey — Results Summary
─────────────────────────────────────────────
 Sites analysed:     {summary.get('total_sites', 0)}
 Avg load (all):     {summary.get('avg_load_all', 0):.0f} ms
 Avg load (nec):     {summary.get('avg_load_nec', 0):.0f} ms
 Avg requests (all): {summary.get('avg_req_all', 0):.0f}
 Avg requests (nec): {summary.get('avg_req_nec', 0):.0f}
─────────────────────────────────────────────
 Reports written to: {config.REPORTS_DIR}
─────────────────────────────────────────────""")


if __name__ == "__main__":
    run_all()
