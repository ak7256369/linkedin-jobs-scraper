#!/usr/bin/env python3
"""Daily last-24-hours pipeline: scrape -> AI/Software filter -> resolve external ATS links -> POST.

Steps
1. scrape_all.py logic: LinkedIn public guest search (US, remote, 24h, senior) -> all_scraped_jobs.csv
2. linkedin_ai_job_filter.process: strict filter (LinkedIn URL used as a placeholder apply link,
   because the real company link is only known after step 3) -> jobs.csv / rejected_jobs.csv
3. resolve_ats_links.py: find the company's own ATS posting and cross-verify every filter
   -> external_links_24h.csv (all resolved) and remote_external_links_24h.csv (PASS only)
4. Collect PASS external_apply_url values (deduped, order preserved) and POST them to the
   ingest endpoint given by TRUESOFTS_INGEST_URL / TRUESOFTS_INGEST_TOKEN (skipped with a
   warning when either is unset). A non-2xx response exits non-zero.

A summary (no secrets) is written to data/last_run.json; the workflow commits it as a keepalive.

Usage: python daily_pipeline.py [--dry-run] [--keywords "kw1" "kw2" ...] [--skip-post]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import scrape_all
from linkedin_ai_job_filter import INPUT_COLUMNS, process

ROOT = Path(__file__).resolve().parent
SCRAPED_CSV = ROOT / "all_scraped_jobs.csv"
FILTER_INPUT_CSV = ROOT / "input_jobs.csv"
FILTER_PASS_CSV = ROOT / "jobs.csv"
FILTER_REJECT_CSV = ROOT / "rejected_jobs.csv"
RESOLVED_CSV = ROOT / "external_links_24h.csv"
REMOTE_CSV = ROOT / "remote_external_links_24h.csv"
URLS_JSON = ROOT / "external_apply_urls.json"
LAST_RUN_JSON = ROOT / "data" / "last_run.json"

KARACHI = ZoneInfo("Asia/Karachi")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def tab_name_for(now: datetime | None = None) -> str:
    """Run date in Asia/Karachi as 'DD Mon' (e.g. '03 Nov'), independent of locale and runner clock zone."""
    local = (now or datetime.now(KARACHI)).astimezone(KARACHI)
    return f"{local.day:02d} {MONTHS[local.month - 1]}"


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def dedupe_keep_order(urls: list[str]) -> list[str]:
    seen, out = set(), []
    for u in urls:
        u = (u or "").strip()
        key = u.rstrip("/").lower()
        if u and key not in seen:
            seen.add(key)
            out.append(u)
    return out


def step_scrape(keywords: list[str] | None) -> list[dict]:
    jobs = scrape_all.scrape_all_jobs(keywords or scrape_all.DEFAULT_KEYWORDS, out_path=str(SCRAPED_CSV))
    print(f"[scrape] {len(jobs)} unique jobs, {scrape_all.FETCH_FAILURES} page fetch failure(s)", flush=True)
    if not jobs and scrape_all.FETCH_FAILURES:
        raise SystemExit("[scrape] ERROR: 0 jobs and LinkedIn page fetches failed - runner IP is probably blocked/limited.")
    return jobs


def step_filter(jobs: list[dict]) -> tuple[int, int]:
    # The strict filter requires an apply link; before resolution the LinkedIn job URL stands in.
    with FILTER_INPUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=INPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for j in jobs:
            row = {c: j.get(c, "") for c in INPUT_COLUMNS}
            row["external_apply_url"] = row["external_apply_url"] or row["linkedin_job_url"]
            w.writerow(row)
    passed, rejected = process(FILTER_INPUT_CSV, FILTER_PASS_CSV, FILTER_REJECT_CSV, allow_linkedin_apply=True)
    print(f"[filter] PASS={passed} REJECT={rejected}", flush=True)
    return passed, rejected


def step_resolve(passed: int) -> None:
    if passed == 0:
        # Nothing to resolve; make sure no stale outputs from a previous local run are reused.
        for p in (RESOLVED_CSV, REMOTE_CSV):
            p.unlink(missing_ok=True)
        print("[resolve] skipped (no jobs passed the filter)", flush=True)
        return
    cmd = [sys.executable, "resolve_ats_links.py", str(FILTER_PASS_CSV.name), str(RESOLVED_CSV.name)]
    print(f"[resolve] running: {' '.join(cmd[1:])}", flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def collect_urls() -> list[str]:
    rows = read_csv(REMOTE_CSV)
    return dedupe_keep_order([r.get("external_apply_url", "") for r in rows if r.get("filter_status", "PASS") == "PASS"])


def post_urls(payload: dict) -> dict | None:
    url = os.environ.get("TRUESOFTS_INGEST_URL", "").strip()
    token = os.environ.get("TRUESOFTS_INGEST_TOKEN", "").strip()
    if not url or not token:
        msg = "TRUESOFTS_INGEST_URL / TRUESOFTS_INGEST_TOKEN not set - skipping POST to the website."
        print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"WARNING: {msg}", flush=True)
        return None

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "linkedin-jobs-scraper/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        raise SystemExit(f"[post] ERROR: ingest endpoint returned HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"[post] ERROR: could not reach ingest endpoint: {exc.reason}")

    if not 200 <= status < 300:
        raise SystemExit(f"[post] ERROR: ingest endpoint returned HTTP {status}: {body[:1000]}")
    try:
        result = json.loads(body)
    except ValueError:
        result = {"raw": body[:1000]}
    print(f"[post] HTTP {status}: {json.dumps(result)}", flush=True)
    return result


def write_last_run(summary: dict) -> None:
    LAST_RUN_JSON.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_JSON.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="send dry_run=true to the ingest endpoint")
    ap.add_argument("--skip-post", action="store_true", help="do not contact the ingest endpoint at all")
    ap.add_argument("--keywords", nargs="+", help="override the default LinkedIn search keywords")
    args = ap.parse_args(argv)

    started = datetime.now(KARACHI)
    tab_name = tab_name_for(started)
    summary = {
        "started_at_pkt": started.isoformat(timespec="seconds"),
        "tab_name": tab_name,
        "source_run": os.environ.get("GITHUB_RUN_ID", "local"),
        "dry_run": args.dry_run,
        "status": "running",
    }
    print(f"Run date (Asia/Karachi): {started:%Y-%m-%d %H:%M} -> tab_name '{tab_name}'", flush=True)

    try:
        jobs = step_scrape(args.keywords)
        summary["scraped"] = len(jobs)
        passed, rejected = step_filter(jobs)
        summary.update(filter_pass=passed, filter_reject=rejected)
        step_resolve(passed)
        summary["resolved"] = len(read_csv(RESOLVED_CSV))
        urls = collect_urls()
        summary["external_urls"] = len(urls)
        URLS_JSON.write_text(json.dumps(urls, indent=2) + "\n", encoding="utf-8")
        print(f"[collect] {len(urls)} verified external apply URLs", flush=True)

        payload = {
            "tab_name": tab_name,
            "header": "external_apply_url",
            "urls": urls,
            "dry_run": args.dry_run,
            "source_run": summary["source_run"],
        }
        if args.skip_post:
            summary["post"] = "skipped (--skip-post)"
        else:
            result = post_urls(payload)
            summary["post"] = result if result is not None else "skipped (secrets not set)"
        summary["status"] = "ok"
        return 0
    except BaseException as exc:  # record the failure for the keepalive file, then re-raise
        summary["status"] = "failed"
        summary["error"] = str(exc)[:500]
        raise
    finally:
        summary["finished_at_pkt"] = datetime.now(KARACHI).isoformat(timespec="seconds")
        write_last_run(summary)


if __name__ == "__main__":
    sys.exit(main())
