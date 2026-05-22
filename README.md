# Kapruka GSC Before/After Monitor

Flask app that pulls Google Search Console data and shows a before-vs-after
comparison of product and category page performance. Branded queries
(kapruka, liquor, pizza, dlb, dbl) and liquor product pages are filtered out.
Positions are impression-weighted; CTR is true clicks/impressions.

## Local run

```bash
pip install -r requirements.txt

export GOOGLE_CLIENT_ID="your-client-id"
export GOOGLE_CLIENT_SECRET="your-secret"
export GSC_REFRESH_TOKEN="your-refresh-token"
export GSC_SITE_URL="https://www.kapruka.com"

python app.py
# open http://localhost:5000
```

## Deploy to Render

1. Push this folder to a GitHub repo.
2. In Render: New > Web Service > connect the repo. It auto-detects `render.yaml`.
3. Set the three secret env vars in the Render dashboard (Environment tab):
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GSC_REFRESH_TOKEN`
4. Deploy. The free tier sleeps after inactivity; first load after sleep is slow.

## How it works

- `GET /api/comparison` — pulls live from GSC and returns the comparison.
  Defaults to current week vs the same week 4 weeks ago. Accepts optional
  `before_start`, `before_end`, `after_start`, `after_end` (YYYY-MM-DD) for
  any custom two-period comparison — this is what the date pickers use.
- `GET /api/defaults` — returns the default date window.
- Every page load and every "Compare" click pulls fresh from the GSC API
  (no caching). Each comparison = 2 API calls. Watch your GSC quota if many
  people hit it at once.

## Notes

- The default window ends 3 days back because GSC "final" data lags ~2-3 days.
- Use the date pickers to compare any two periods (e.g. before vs after a
  specific launch date). Click "Reset to default" to return to the weekly view.
- To change which terms are filtered, edit `NOISE_PATTERN` and `LIQUOR_PAGE`
  near the top of `app.py`.
- Page-type tagging: `/buyonline/` = product, `/online/` = category. Adjust
  `PRODUCT_RE` / `CATEGORY_RE` if your URL structure differs.
