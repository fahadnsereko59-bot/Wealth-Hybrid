WEALTH HYBRID V86.41 — API-FOOTBALL ONLY

Provider isolation: app.py contains no ESPN requests. All fixtures, recent team form and head-to-head data come only from API-Football v3.

Railway Variables:
- API_FOOTBALL_KEY: use a newly regenerated key; the key shown in your screenshot is exposed.
- WEALTH_PASSWORD: set your own login password (default qwerty is demo-only).
- SECRET_KEY: stable long random secret.
- DATA_DIR: /data (Railway Volume mounted at /data); falls back to current working directory if /data is not writable.
- API_FOOTBALL_DAILY_CAP: default 90 app-level calls per Kampala calendar day, leaving about 10 of a 100/day plan for other uses and provider overhead.
- SPORTS_CACHE_TTL: default 43200 seconds (12 hours) for daily fixture responses.
- PORT: read automatically from Railway.

Database: /data/wealth.db when DATA_DIR=/data is writable, otherwise ./wealth.db. Tables include cache(key,data,ts) and api_usage(day,calls). Every API-Football request includes timezone=Africa/Kampala.

Fixtures: the scoreboard fetches today plus the next six Kampala dates. Each date query returns all fixtures supplied by API-Football; no 20-match truncation and no invented fixtures. Each uncached date costs one API request. Fixture cache defaults to 12 hours.

History: when a user taps “View form / H2H”, the app lazily requests each team's last five fixtures and the teams' last five head-to-head meetings. These results are cached for 7 days. First-time history can use up to 3 additional requests per match, with repeated team/pair history requests served from SQLite. The shared daily cap applies to fixtures and history alike; when exhausted, stale cached data is used if available.

Coverage depends on API-Football plan and availability. “All leagues” means all fixtures returned by API-Football for that date, not every league worldwide regardless of provider coverage. Live network access has to be verified on Railway.

Deploy the CONTENTS of this folder at the repository root. Procfile runs `gunicorn app:app`.
