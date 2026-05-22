"""
Kapruka GSC Before/After Monitor
Flask backend that pulls Search Console data, filters branded + liquor noise,
and computes weighted before-vs-after comparisons for product & category pages.
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

# Branded + known non-organic query terms
NOISE_PATTERN = re.compile(r"\b(?:kapruka|liq[ou]r|liquor|pizza|dlb|dbl)\b", re.IGNORECASE)

# Liquor product PAGES (filtered by URL, not just query)
LIQUOR_PAGE = re.compile(
    r"(?:liq|whisky|whiskey|brandy|vodka|arrack|beer|wine|rum|gin|champagne|cognac|"
    r"scotch|bourbon|abv|somersby|carlsberg|heineken|jack.daniel|johnnie.walker|"
    r"old.keg|vat.9|franklin|roskaa|navy.seal|tillsider|rockland|lion.lager|sir.edwards)",
    re.IGNORECASE,
)

PRODUCT_RE  = re.compile(r"/buyonline/")
CATEGORY_RE = re.compile(r"/online/")


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


# ─────────────────────────────────────────────────────────────────────────────
# FIX #1: Paginated GSC fetch — pulls ALL rows instead of capping at 25K
# ─────────────────────────────────────────────────────────────────────────────
def fetch_gsc(service, start, end, row_limit=25000, max_pages=4):
    """
    Paginate through GSC API results using startRow.
    The API returns max 25,000 rows per request. Cap at max_pages iterations
    (default 4 = 100K rows) to stay within gunicorn worker timeout.
    """
    all_records = []
    start_row = 0
    page = 0

    while page < max_pages:
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": ["date", "query", "page"],
            "rowLimit": row_limit,
            "startRow": start_row,
            "dataState": "final",
        }
        resp = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()
        rows = resp.get("rows", [])

        for r in rows:
            all_records.append({
                "date": r["keys"][0],
                "query": r["keys"][1],
                "page": r["keys"][2],
                "clicks": r.get("clicks", 0),
                "impressions": r.get("impressions", 0),
                "position": r.get("position", 0),
            })

        # If we got fewer rows than the limit, we've reached the end
        if len(rows) < row_limit:
            break

        start_row += row_limit
        page += 1

    return pd.DataFrame(all_records)


def clean_and_tag(df):
    if df.empty:
        return df
    df = df[~df["query"].str.contains(NOISE_PATTERN, regex=True, na=False)].copy()
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
            "unique_queries": int(sub["query"].nunique()),
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


def page_movers(df_before, df_after, page_type, n=10):
    def psum(df):
        sub = df[df["page_type"] == page_type]
        if sub.empty:
            return pd.DataFrame(columns=["page", "clicks", "impressions", "pos_weighted"])
        g = (
            sub.groupby("page")
            .agg(clicks=("clicks", "sum"), impressions=("impressions", "sum"),
                 pos_weighted=("pos_weighted", "sum"))
            .reset_index()
        )
        g["avg_position"] = (g["pos_weighted"] / g["impressions"]).round(2)
        return g

    b = psum(df_before)
    a = psum(df_after)
    if a.empty and b.empty:
        return [], [], [], []
    m = a.merge(b, on="page", suffixes=("_after", "_before"), how="outer").fillna(0)
    # Ensure all expected columns exist even if one side was empty
    num_cols = ["clicks_after", "clicks_before", "impressions_after",
                "impressions_before", "avg_position_after", "avg_position_before"]
    for col in num_cols:
        if col not in m.columns:
            m[col] = 0
        m[col] = pd.to_numeric(m[col], errors="coerce").fillna(0)
    m["click_change"] = m["clicks_after"] - m["clicks_before"]
    # Higher position number = worse rank, so a positive change = dropped
    m["pos_change"] = m["avg_position_after"] - m["avg_position_before"]

    def fmt(row):
        return {
            "page": row["page"].replace("https://www.kapruka.com", ""),
            "clicks_before": int(row["clicks_before"]),
            "clicks_after": int(row["clicks_after"]),
            "click_change": int(row["click_change"]),
            "pos_before": round(row["avg_position_before"], 1),
            "pos_after": round(row["avg_position_after"], 1),
            "pos_change": round(row["pos_change"], 1),
            "impressions_before": int(row["impressions_before"]),
            "impressions_after": int(row["impressions_after"]),
        }

    gainers = [fmt(r) for _, r in m.nlargest(n, "click_change").iterrows()]
    losers = [fmt(r) for _, r in m.nsmallest(n, "click_change").iterrows()]

    # Position drops: only pages that ranked in BOTH periods (real comparison)
    # and had meaningful visibility, sorted by biggest rank drop.
    ranked_both = m[
        (m["avg_position_before"] > 0)
        & (m["avg_position_after"] > 0)
        & (m["impressions_before"] >= 10)
        & (m["impressions_after"] >= 10)
    ]
    pos_droppers = [
        fmt(r) for _, r in ranked_both.nlargest(n, "pos_change").iterrows()
        if r["pos_change"] > 0
    ]

    # Emerging pages: had ZERO clicks in the before period, now getting clicks.
    # Brand-new winners that didn't exist in the comparison window.
    emerging_df = m[(m["clicks_before"] == 0) & (m["clicks_after"] > 0)]
    emerging = [fmt(r) for _, r in emerging_df.nlargest(n, "clicks_after").iterrows()]

    return gainers, losers, pos_droppers, emerging


# ─────────────────────────────────────────────────────────────────────────────
# FIX #2: Surface row counts + pagination info so you can verify completeness
# ─────────────────────────────────────────────────────────────────────────────
def compute_comparison(before_start, before_end, after_start, after_end):
    service = _gsc_service()
    raw_before_full = fetch_gsc(service, before_start, before_end)
    raw_after_full = fetch_gsc(service, after_start, after_end)

    # Track raw row counts before filtering
    rows_before_raw = len(raw_before_full)
    rows_after_raw = len(raw_after_full)

    raw_before = clean_and_tag(raw_before_full)
    raw_after = clean_and_tag(raw_after_full)

    rows_before_clean = len(raw_before)
    rows_after_clean = len(raw_after)

    sum_b = summarize(raw_before)
    sum_a = summarize(raw_after)

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

    cat_gainers, cat_losers, cat_pos_drops, cat_emerging = page_movers(raw_before, raw_after, "category")
    prod_gainers, prod_losers, prod_pos_drops, prod_emerging = page_movers(raw_before, raw_after, "product")

    return {
        "periods": {
            "before": {"start": before_start, "end": before_end},
            "after": {"start": after_start, "end": after_end},
        },
        "filters": {
            "queries": ["kapruka", "liquor / liqor", "pizza", "dlb", "dbl"],
            "liquor_pages": [
                "whisky", "brandy", "vodka", "arrack", "beer", "wine", "rum",
                "gin", "champagne", "cognac", "scotch", "bourbon", "and named "
                "liquor brands (Old Keg, VAT 9, Carlsberg, Heineken, etc.)",
            ],
        },
        # Row counts for transparency — verify data completeness
        "row_counts": {
            "before_raw": rows_before_raw,
            "before_after_filters": rows_before_clean,
            "after_raw": rows_after_raw,
            "after_after_filters": rows_after_clean,
        },
        "comparison": comparison,
        "daily": {
            "category_before": daily_series(raw_before, "category"),
            "category_after": daily_series(raw_after, "category"),
            "product_before": daily_series(raw_before, "product"),
            "product_after": daily_series(raw_after, "product"),
        },
        "movers": {
            "category": {"gainers": cat_gainers, "losers": cat_losers,
                         "pos_drops": cat_pos_drops, "emerging": cat_emerging},
            "product": {"gainers": prod_gainers, "losers": prod_losers,
                        "pos_drops": prod_pos_drops, "emerging": prod_emerging},
        },
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


# ─────────────────────────────────────────────────────────────────────────────
# FIX #3: Push "after" window back to 4 days instead of 3 for safer final data
# ─────────────────────────────────────────────────────────────────────────────
def default_periods():
    today = datetime.date.today()
    # GSC data takes 2-4 days to finalize. Using 4-day lag to avoid
    # partial data in the "after" window understating real numbers.
    lag = 4
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
