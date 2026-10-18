WEALTH HYBRID — RAILWAY PACKAGE

Deploy:
1. Upload the CONTENTS of this folder to the root of your GitHub repository.
2. Railway should detect nixpacks.toml. Build installs Python dependencies and Playwright Chromium.
3. Start command: gunicorn app:app --workers 1 --threads 4 --timeout 120
4. Set WEALTH_PASSWORD and SECRET_KEY in Railway Variables if your app version uses them.
5. Mount a Railway Volume at /data if persistent storage is implemented/needed.
6. Visit /api/diagnosis after deploy.

IMPORTANT DATA LIMITATION:
This source originally used LiveScore scraping and a synthetic fallback list. The fallback data is demo/sample data, not live fixtures. It is explicitly identified as demo data in fixture IDs and diagnosis. Do not treat sample scores/stats as real. Websites can change their HTML or block automated browsers, so Playwright scraping cannot be guaranteed. Prefer a licensed football API for reliable fixture/history data. This package adds Playwright Chromium build setup, but live target-site scraping still requires a live Railway verification.

Security:
- Never commit API keys or passwords. Use Railway Variables.
- The API key visible in a screenshot should be regenerated.
- A client-side lock screen is not server-side authentication.
