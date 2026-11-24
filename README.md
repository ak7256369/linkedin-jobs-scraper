# LinkedIn 24h Remote Job Links

Finds senior, US, fully-remote AI / software jobs posted on LinkedIn in the last 24 hours, resolves each one to
the company's **own** application page (Greenhouse, Ashby, Lever, Workday, SmartRecruiters, company career
sites, ...), cross-verifies every filter on that page, and sends the verified links to a website sheet.

## Daily automation (GitHub Actions)

Workflow: [`.github/workflows/daily-scrape.yml`](.github/workflows/daily-scrape.yml)

- **Schedule:** **12:00 AM Pakistan time, Monday–Saturday**, started by an external timer (cron-job.org calls the `workflow_dispatch` API with a fine-grained token that has Actions read/write on this repo only), because GitHub starts scheduled runs hours late. The workflow's own `schedule` triggers (19:05–23:15 UTC) are backups; the gate job skips them when that day's run already ran.
  GitHub may start scheduled runs a few minutes late.
- **Manual run:** Actions → *Daily 24h LinkedIn external links* → *Run workflow*, with optional `dry_run`.
- **Jobs:** `test` → `scrape` (4 parallel shards) → `finish` (merge, filter, resolve, POST).
- **Rate limits:** LinkedIn throttles bursts per IP with HTTP 429. The scraper handles it three ways:
  - the keywords are split over 4 matrix jobs, each on its own runner VM and IP;
  - on 429 it honours `Retry-After` or backs off 20 → 40 → 80 → 160 s and slows its page pace;
  - a page that still fails is resumed from that offset in up to 3 later passes (2, 4, 6 min cool-downs)
    instead of being dropped.
  Keywords still incomplete after that are listed as a warning and in `data/last_run.json`.
- **Pipeline:** [`daily_pipeline.py`](daily_pipeline.py)
  1. `scrape_all.py` – LinkedIn public guest search (no login), 24h, remote, US, senior.
  2. `linkedin_ai_job_filter.py` – strict filter (blacklist, Director/VP, seniority, AI/software domain, age...).
  3. `resolve_ats_links.py` – finds the company's own posting and re-checks remote/US/seniority/domain on it.
  4. Verified `external_apply_url`s (deduped, order kept) are POSTed to the website.
- **Outputs:** all CSVs plus `external_apply_urls.json` are uploaded as the run's artifact (30 days).
- **Keepalive:** each run commits `data/last_run.json` (counts and status only), so GitHub never disables the
  schedule for inactivity.

### Required repository secrets

| Secret | Purpose |
| --- | --- |
| `TRUESOFTS_INGEST_URL` | Ingest endpoint that adds the links to the "LinkedIn Work" sheet |
| `TRUESOFTS_INGEST_TOKEN` | Bearer token for that endpoint |

If either is missing, the run still scrapes and uploads artifacts, and skips the POST with a warning.
A non-2xx answer from the endpoint fails the run.

POST body: `{"tab_name": "19 Nov", "header": "external_apply_url", "urls": [...], "dry_run": false, "source_run": "<run id>"}`
– `tab_name` is the run date in Asia/Karachi time.

## Run locally

```bash
pip install -r requirements.txt
python daily_pipeline.py --skip-post          # full pipeline, no website call
python daily_pipeline.py --keywords "senior ai remote" --skip-post
python daily_pipeline.py --scrape-only shards --shard 0 --shards 4   # what each CI shard runs
python daily_pipeline.py --merge shards --skip-post                  # what the finish job runs
python -m unittest discover tests
```

Individual tools:

```bash
python scrape_all.py                           # -> all_scraped_jobs.csv
python resolve_ats_links.py jobs.csv external_links_24h.csv
python linkedin_ai_job_filter.py --sample      # filter demo on built-in sample rows
python linkedin_ai_job_filter.py --input input_jobs.csv --output jobs.csv --rejected rejected_jobs.csv --allow-linkedin-apply
```

Windows shortcuts: `run_scraper.bat`, `run_filter.bat`, `open_search.bat`.

## Output files (generated, not committed)

- `all_scraped_jobs.csv` – raw LinkedIn results after the first-pass filter.
- `jobs.csv` / `rejected_jobs.csv` – strict filter PASS rows / rejected rows with reasons.
- `external_links_24h.csv` – every resolved external posting with PASS/REJECT and evidence.
- `remote_external_links_24h.csv`, `external_links.csv` – verified PASS rows only.

## Filters

US only; explicitly fully remote (hybrid/on-site rejected); Senior / Lead / Staff / Principal; no Director/VP;
AI or software domain; engineering roles only (no product/program managers, designers, recruiters, sales or specialist roles); Director/VP also checked in the company URL slug; posted in the last 24 hours; real non-LinkedIn HTTPS apply link; not an aggregator; company
not blacklisted (ClickUp, Jobright.ai, Alignerr, Ladders, Rex.zone, Jobtogether/Jobgether, Dayforce, Micro1,
Data Annotation, Turing, ChatGPT Jobs, Synthesia).

## Notes

This uses LinkedIn's public, logged-out job search pages. Automated access may conflict with LinkedIn's
[User Agreement](https://www.linkedin.com/legal/user-agreement); LinkedIn can rate-limit or block the requests,
in which case the run fails with a clear message instead of sending an empty list.
