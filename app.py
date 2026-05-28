"""
Kapruka GSC Before/After Monitor
Flask backend that pulls Search Console data, filters branded + liquor noise,
and computes weighted before-vs-after comparisons for product & category pages.

Data accuracy notes:
- Branded query filtering done at GSC API level (notContains)
- Liquor page filtering done post-fetch (URL regex)
- KPI totals use page-level aggregation (fewer rows, complete data)
- Site property: https://www.kapruka.com (includes all paths: /online/, /lk/online/, etc.)
"""
import os
import re
import datetime
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

app = Flask(__name__, static_folder="static")

# ─────────────────────────────────────────────────────────────────────────────
# Config — set these as environment variables on Render
# ─────────────────────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GSC_REFRESH_TOKEN    = os.environ.get("GSC_REFRESH_TOKEN", "")
GSC_SITE_URL         = os.environ.get("GSC_SITE_URL", "https://www.kapruka.com")

# ─────────────────────────────────────────────────────────────────────────────
# Filters
# ─────────────────────────────────────────────────────────────────────────────

# Branded/noise query terms — excluded at the GSC API level
BRANDED_QUERY_TERMS = ["kapruka", "liquor", "liqor", "pizza", "dlb", "dbl"]

# Liquor product PAGES — excluded post-fetch by URL pattern
LIQUOR_PAGE = re.compile(
    r"(?:liq|whisky|whiskey|brandy|vodka|arrack|beer|wine|rum|gin|champagne|cognac|"
    r"scotch|bourbon|abv|somersby|carlsberg|heineken|jack.daniel|johnnie.walker|"
    r"old.keg|vat.9|franklin|roskaa|navy.seal|tillsider|rockland|lion.lager|sir.edwards)",
    re.IGNORECASE,
)

# Page type detection — matches both /online/ and /lk/online/, /buyonline/ and /lk/buyonline/
PRODUCT_RE  = re.compile(r"/buyonline/")
CATEGORY_RE = re.compile(r"/online/")

# Pages to exclude: /lk/ prefixed paths (we only track kapruka.com domain paths)
EXCLUDE_LK = re.compile(r"kapruka\.com/lk[st]?/", re.IGNORECASE)


def _gsc_service():
    creds = Credentials(
        token=None,
        refresh_token=GSC_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _brand_filters():
    """
    GSC API dimension filters to exclude branded/noise queries.
    Filters within a single group are ANDed.
    """
    filters = []
    for term in BRANDED_QUERY_TERMS:
        filters.append({
            "dimension": "query",
            "operator": "notContains",
            "expression": term,
        })
    return [{"filters": filters}]


def fetch_gsc_pages(service, start, end, row_limit=25000):
    """
    Fetch page-level data (dimensions: date + page only).
    This produces far fewer rows than date+query+page, so we can
    get complete data without hitting row limits.
    Used for: KPI totals, daily series, position tracking.
    """
    all_records = []
    start_row = 0
    dim_filters = _brand_filters()

    while True:
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["date", "page"],
            "dimensionFilterGroups": dim_filters,
            "rowLimit": row_limit,
            "startRow": start_row,
            "dataState": "final",
        }
        resp = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()
        rows = resp.get("rows", [])

        for r in rows:
            all_records.append({
                "date": r["keys"][0],
                "page": r["keys"][1],
                "clicks": r.get("clicks", 0),
                "impressions": r.get("impressions", 0),
                "position": r.get("position", 0),
            })

        if len(rows) < row_limit:
            break
        start_row += row_limit
        # Safety: 5 pages = 125K rows
        if start_row >= row_limit * 5:
            break

    return pd.DataFrame(all_records)


def fetch_gsc_queries(service, start, end, row_limit=25000):
    """
    Fetch query+page level data (dimensions: query + page).
    Used only for unique-query counts in the KPI cards.
    """
    all_records = []
    start_row = 0
    dim_filters = _brand_filters()

    while True:
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["query", "page"],
            "dimensionFilterGroups": dim_filters,
            "rowLimit": row_limit,
            "startRow": start_row,
            "dataState": "final",
        }
        resp = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()
        rows = resp.get("rows", [])

        for r in rows:
            all_records.append({
                "query": r["keys"][0],
                "page": r["keys"][1],
                "clicks": r.get("clicks", 0),
                "impressions": r.get("impressions", 0),
                "position": r.get("position", 0),
            })

        if len(rows) < row_limit:
            break
        start_row += row_limit
        if start_row >= row_limit * 5:
            break

    return pd.DataFrame(all_records)


def clean_and_tag(df):
    """
    Post-fetch filtering and tagging.
    - Removes /lk/ prefixed paths (only tracking kapruka.com paths)
    - Removes liquor product pages
    - Tags remaining pages as product/category/other
    """
    if df.empty:
        return df
    # Exclude /lk/, /lks/, /lkt/ prefixed paths
    df = df[~df["page"].str.contains(EXCLUDE_LK, na=False)].copy()
    # Exclude liquor pages
    df = df[~df["page"].str.contains(LIQUOR_PAGE, na=False)].copy()

    def tag(url):
        if PRODUCT_RE.search(url):
            return "product"
        if CATEGORY_RE.search(url):
            return "category"
        return "other"

    df["page_type"] = df["page"].apply(tag)
    df["pos_weighted"] = df["position"] * df["impressions"]
    return df


def summarize(df):
    """Per page_type totals with weighted position + true CTR."""
    out = {}
    for pt in ["product", "category", "ALL"]:
        sub = df if pt == "ALL" else df[df["page_type"] == pt]
        clicks = int(sub["clicks"].sum())
        imp = int(sub["impressions"].sum())
        pos = round(sub["pos_weighted"].sum() / imp, 2) if imp else 0
        ctr = round(clicks / imp, 4) if imp else 0
        out[pt] = {
            "clicks": clicks,
            "impressions": imp,
            "avg_position": pos,
            "ctr": ctr,
            "unique_pages": int(sub["page"].nunique()),
            "unique_queries": 0,  # Not available in page-level fetch
        }
    return out


def daily_series(df, page_type):
    sub = df[df["page_type"] == page_type]
    if sub.empty:
        return []
    g = (
        sub.groupby("date")
        .agg(clicks=("clicks", "sum"), impressions=("impressions", "sum"),
             pos_weighted=("pos_weighted", "sum"))
        .reset_index()
        .sort_values("date")
    )
    g["avg_position"] = (g["pos_weighted"] / g["impressions"]).round(2)
    g["ctr"] = (g["clicks"] / g["impressions"]).round(4)
    return g[["date", "clicks", "impressions", "avg_position", "ctr"]].to_dict("records")


def compute_comparison(before_start, before_end, after_start, after_end):
    service = _gsc_service()

    # ── Page-level fetch: complete data for KPIs, charts, positions ──
    pages_before = clean_and_tag(fetch_gsc_pages(service, before_start, before_end))
    pages_after = clean_and_tag(fetch_gsc_pages(service, after_start, after_end))

    sum_b = summarize(pages_before)
    sum_a = summarize(pages_after)

    # ── Query-level fetch: only for unique-query counts ──
    queries_before = clean_and_tag(fetch_gsc_queries(service, before_start, before_end))
    queries_after = clean_and_tag(fetch_gsc_queries(service, after_start, after_end))

    # Unique query counts from query-level data
    for pt in ["product", "category", "ALL"]:
        for label, qdf in [("before", queries_before), ("after", queries_after)]:
            sub = qdf if pt == "ALL" else qdf[qdf["page_type"] == pt]
            target = sum_b if label == "before" else sum_a
            target[pt]["unique_queries"] = int(sub["query"].nunique()) if "query" in sub.columns else 0

    def pct(a, b):
        return round((a / b - 1) * 100, 1) if b else 0

    comparison = {}
    for pt in ["product", "category", "ALL"]:
        b, a = sum_b[pt], sum_a[pt]
        comparison[pt] = {
            "before": b,
            "after": a,
            "click_change_pct": pct(a["clicks"], b["clicks"]),
            "imp_change_pct": pct(a["impressions"], b["impressions"]),
            "pos_change": round(a["avg_position"] - b["avg_position"], 2),
            "ctr_change_pp": round((a["ctr"] - b["ctr"]) * 100, 2),
        }

    # Build daily series BEFORE freeing page data
    daily = {
        "category_before": daily_series(pages_before, "category"),
        "category_after": daily_series(pages_after, "category"),
        "product_before": daily_series(pages_before, "product"),
        "product_after": daily_series(pages_after, "product"),
    }

    # Row counts for transparency
    page_rows_b = len(pages_before)
    page_rows_a = len(pages_after)
    query_rows_b = len(queries_before)
    query_rows_a = len(queries_after)

    # Free memory
    del pages_before, pages_after, queries_before, queries_after

    return {
        "periods": {
            "before": {"start": before_start, "end": before_end},
            "after": {"start": after_start, "end": after_end},
        },
        "filters": {
            "queries": BRANDED_QUERY_TERMS,
            "liquor_pages": [
                "whisky", "brandy", "vodka", "arrack", "beer", "wine", "rum",
                "gin", "champagne", "cognac", "scotch", "bourbon", "and named "
                "liquor brands (Old Keg, VAT 9, Carlsberg, Heineken, etc.)",
            ],
            "excluded_paths": ["/lk/", "/lks/", "/lkt/"],
        },
        "row_counts": {
            "page_level_before": page_rows_b,
            "page_level_after": page_rows_a,
            "query_level_before": query_rows_b,
            "query_level_after": query_rows_a,
        },
        "comparison": comparison,
        "daily": daily,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def default_periods():
    today = datetime.date.today()
    lag = 7  # GSC finalization safety: ensures the after-window is fully aggregated
    after_end = today - datetime.timedelta(days=lag)
    after_start = today - datetime.timedelta(days=lag + 6)
    before_end = today - datetime.timedelta(days=lag + 28)
    before_start = today - datetime.timedelta(days=lag + 6 + 28)
    return (before_start.isoformat(), before_end.isoformat(),
            after_start.isoformat(), after_end.isoformat())


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/comparison")
def api_comparison():
    bs = request.args.get("before_start")
    be = request.args.get("before_end")
    as_ = request.args.get("after_start")
    ae = request.args.get("after_end")

    if not all([bs, be, as_, ae]):
        bs, be, as_, ae = default_periods()

    try:
        data = compute_comparison(bs, be, as_, ae)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify(data)


@app.route("/api/defaults")
def api_defaults():
    bs, be, as_, ae = default_periods()
    return jsonify({
        "before_start": bs, "before_end": be,
        "after_start": as_, "after_end": ae,
    })


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
