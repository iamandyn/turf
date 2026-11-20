"""Ad-account connector: sync a brand's results from an ad platform's API.

There's no real ad account behind the demo, so ``platform.py`` is a simulated
ad platform (an insights API shaped like Meta's) serving the sample brands'
synthetic results day by day. Everything on Turf's side of the HTTP boundary
is what a production connector needs:

- ``client.py``: the HTTP client. OAuth token with refresh, cursor paging,
  retries with backoff that honor Retry-After, response validation.
- ``store.py``: SQLite state shared by every uvicorn worker: connections,
  sync jobs and their progress events, and the synced daily rows (upserted, so
  re-fetching a day is idempotent).
- ``sync.py``: one sync job: backfill or incremental with a lookback window
  for late-arriving conversions, then the report recomputed and compared with
  the previous one.
- ``routes.py``: the API (``/api/connections``) and the live progress stream
  (server-sent events), which any worker can serve because progress lives in
  the store.
"""
