import sys
import os
import re
import csv
import time
import random
import socket
import urllib.error
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup
from linkedin_ai_job_filter import (
    is_blacklisted_company,
    is_ai_domain,
    is_software_domain,
    is_software_or_ai_domain,
    is_us_country,
    is_remote_workplace,
    parse_minutes,
)

socket.setdefaulttimeout(15)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# Word-boundary rules: plain substring checks ("ai" in title) matched words like
# "ClaimCenter", "Blockchain", "Retail" and "Email", letting non-AI roles through.
ROLE_DOMAIN_RULES = [
    ("Generative AI", r"\b(generative\s+ai|gen[\s-]?ai|llms?)\b"),
    ("Computer Vision", r"\b(computer\s+vision|vision)\b"),
    ("NLP", r"\b(natural\s+language|nlp)\b"),
    ("MLOps", r"\bmlops\b"),
    ("Machine Learning", r"\b(machine\s+learning|ml)\b"),
    ("Artificial Intelligence", r"\b(applied\s+ai|ai|artificial\s+intelligence)\b"),
    ("Data Science / Machine Learning", r"\b(data\s+scien(?:tist|ce))\b"),
    ("Software Engineering", r"\b(software|backend|back[\s-]end|frontend|front[\s-]end|full[\s-]?stack|swe)\b"),
    ("Platform / Systems Engineering", r"\b(platform|infrastructure|systems|cloud)\b"),
]

# Number of LinkedIn pages that could still not be fetched after every retry and resume pass.
FETCH_FAILURES = 0
# Keywords whose results are incomplete after all passes: [{"keyword": ..., "offset": ...}]
INCOMPLETE = []
# Total HTTP 429 responses seen (for run summaries).
THROTTLE_HITS = 0

# --- Rate-limit handling -------------------------------------------------------------
# LinkedIn answers bursts from one IP with HTTP 429 for roughly a minute or more. Short
# retries (2-8 s) just burn attempts inside that window, so on 429 we honour Retry-After
# or back off exponentially (20 s -> 40 s -> 80 s -> 160 s, capped, with jitter) and slow
# the normal page pace for the rest of the run (it then recovers slowly on success).
# A page that still fails is not dropped: its keyword is resumed from that offset in a
# later pass after a cool-down.
BACKOFF_BASE = float(os.environ.get("SCRAPE_BACKOFF_BASE", "20"))
BACKOFF_CAP = float(os.environ.get("SCRAPE_BACKOFF_CAP", "180"))
MAX_429_ATTEMPTS = int(os.environ.get("SCRAPE_MAX_429_ATTEMPTS", "5"))
MAX_ERROR_ATTEMPTS = 3
PACE_MIN = float(os.environ.get("SCRAPE_PACE_MIN", "1.5"))
PACE_MAX = 12.0
RESUME_PASSES = int(os.environ.get("SCRAPE_RESUME_PASSES", "3"))
RESUME_COOLDOWN = float(os.environ.get("SCRAPE_RESUME_COOLDOWN", "120"))
_pace = PACE_MIN


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def _retry_after_seconds(exc) -> float | None:
    try:
        value = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def fetch_page(url: str, ua: str):
    """Fetch one LinkedIn page. Returns HTML (possibly '' for an empty page) or None on failure."""
    global _pace, THROTTLE_HITS
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    throttled = errors = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", "replace")
            _pace = max(PACE_MIN, _pace * 0.9)  # recover slowly after throttling
            return html
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code == 999 or exc.code >= 500:
                if exc.code == 429:
                    THROTTLE_HITS += 1
                throttled += 1
                _pace = min(PACE_MAX, _pace * 2)
                if throttled >= MAX_429_ATTEMPTS:
                    print(f"  HTTP {exc.code} x{throttled} - giving up on this page for now.", flush=True)
                    return None
                wait = _retry_after_seconds(exc) or min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (throttled - 1))
                wait *= random.uniform(0.85, 1.25)
                print(f"  HTTP {exc.code} (attempt {throttled}/{MAX_429_ATTEMPTS}); backing off {wait:.0f}s, "
                      f"page pace now {_pace:.1f}s", flush=True)
                _sleep(wait)
                continue
            if exc.code in (400, 404):
                return ""  # past the end of the result list
            errors += 1
            exc_msg = f"HTTP {exc.code}"
        except Exception as exc:  # timeouts, connection resets, DNS
            errors += 1
            exc_msg = str(exc)
        if errors >= MAX_ERROR_ATTEMPTS:
            print(f"  {exc_msg} x{errors} - giving up on this page for now.", flush=True)
            return None
        wait = 5 * errors * random.uniform(0.8, 1.3)
        print(f"  {exc_msg}; retrying in {wait:.0f}s", flush=True)
        _sleep(wait)


def classify_role_domain(title: str) -> str:
    for label, pattern in ROLE_DOMAIN_RULES:
        if re.search(pattern, title or "", re.IGNORECASE):
            return label
    return "Other"


def scrape_jobs_for_keyword(keyword: str, geoid: str = "103644278", f_tpr: str = "r86400", f_wt: str = "2", f_e: str = "4",
                            seen_ids: set = None, start_offset: int = 0, return_failed_offset: bool = False):
    """Scrape one keyword. With return_failed_offset=True returns (jobs, offset_or_None), where the
    offset is the page that could not be fetched (so the keyword can be resumed later)."""
    if seen_ids is None:
        seen_ids = set()

    base_api = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    jobs = []
    start = start_offset
    failed_offset = None

    resumed = f", resuming at offset {start_offset}" if start_offset else ""
    print(f"\n--- Scraping jobs for keyword='{keyword}' (US, Remote, 24h, 5+ YOE [f_E={f_e}]){resumed} ---", flush=True)

    consecutive_empty = 0
    while start < 1000:
        query_params = {
            "keywords": keyword,
            "geoId": geoid,
            "f_TPR": f_tpr,
            "f_WT": f_wt,
            "start": start,
        }
        if f_e:
            query_params["f_E"] = f_e
        query = urllib.parse.urlencode(query_params)
        api_url = f"{base_api}?{query}"
        ua = USER_AGENTS[(start // 10) % len(USER_AGENTS)]

        html = fetch_page(api_url, ua)
        if html is None:
            failed_offset = start
            print(f"Could not fetch offset {start}; keyword will be resumed from here in a later pass.", flush=True)
            break

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", class_="job-search-card")
        if not cards:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                print("End of search results reached for keyword.")
                break
            start += 10
            _sleep(_pace)
            continue

        consecutive_empty = 0
        new_on_page = 0
        for card in cards:
            title_el = card.find("h3", class_="base-search-card__title")
            comp_el = card.find("h4", class_="base-search-card__subtitle")
            loc_el = card.find("span", class_="job-search-card__location")
            time_el = card.find("time")
            link_el = card.find("a", class_="base-card__full-link")

            title = title_el.get_text(strip=True) if title_el else ""
            company = comp_el.get_text(strip=True) if comp_el else ""
            location = loc_el.get_text(strip=True) if loc_el else "United States"
            time_str = time_el.get_text(strip=True) if time_el else "120"
            link = link_el.get("href", "") if link_el else ""
            clean_url = link.split("?")[0] if link else ""

            m_id = re.search(r"-(\d+)(?:$|\?)", clean_url) or re.search(r"/view/(\d+)", clean_url)
            job_id = m_id.group(1) if m_id else clean_url

            if not job_id or job_id in seen_ids or not title or not company:
                continue
            seen_ids.add(job_id)

            # 0. Enforce strictly last 24 hours
            mins = parse_minutes(time_str)
            if (mins is not None and mins > 1440) or re.search(r"\b(weeks?|months?|years?)\b", time_str, re.IGNORECASE):
                continue

            # 1. Skip blacklisted companies right away
            if is_blacklisted_company(company=company, title=title, url=clean_url)[0]:
                continue

            # 2. Skip explicitly hybrid/onsite in title or location
            if re.search(r"\b(hybrid|on-site|onsite|in-office|in person|in-person)\b", f"{title} {location}", re.IGNORECASE):
                continue

            # 3. Skip Director / VP / Vice roles
            if re.search(r"\b(director|vp|vice\s*president|vice)\b", title, re.IGNORECASE):
                continue

            # 4. Skip non-US location if explicitly tagged outside US
            loc_lower = location.lower()
            if any(country in loc_lower for country in [
                "canada", "united kingdom", "india", "germany", "france",
                "australia", "poland", "brazil", "spain", "mexico", "ireland", "netherlands"
            ]):
                continue

            # 5. Must be Senior level
            has_sr = bool(re.search(r"\b(senior|sr|lead|staff|principal|architect|head)\b", title, re.IGNORECASE))
            if not has_sr:
                continue

            seniority = "Senior"
            role_domain = classify_role_domain(title)

            # 6. Must match AI or Software domain
            if not is_software_or_ai_domain(role_domain) and not is_software_or_ai_domain(title):
                continue

            jobs.append({
                "job_title": title,
                "company": company,
                "country": "United States",
                "workplace_type": "Remote",
                "seniority": seniority,
                "role_domain": role_domain,
                "linkedin_job_url": clean_url,
                "external_apply_url": "",  # Will be resolved to direct company/ATS link
                "verified_badge": "yes",
                "posted_minutes_ago": time_str,
                "notes": f"Scraped from LinkedIn search (Location: {location})",
            })
            new_on_page += 1

        print(f"Offset {start}: +{new_on_page} unique jobs. Total keyword collected: {len(jobs)}")
        start += len(cards)
        if len(cards) < 10:
            print("Reached last page.")
            break
        _sleep(_pace * random.uniform(0.8, 1.3))

    if return_failed_offset:
        return jobs, failed_offset
    if failed_offset is not None:
        global FETCH_FAILURES
        FETCH_FAILURES += 1
    return jobs

DEFAULT_KEYWORDS = [
    "senior software remote",
    "senior ai remote",
    "senior backend remote",
    "senior full stack remote",
    "staff software remote",
    "senior machine learning remote",
    "lead software remote",
    "principal software remote",
    "senior data engineer remote",
    "senior frontend remote",
    "staff machine learning remote",
    "senior devops remote",
    "senior cloud engineer remote",
    "principal ai remote",
]

def save_all_scraped(jobs, path="all_scraped_jobs.csv"):
    fieldnames = [
        "job_title", "company", "country", "workplace_type", "seniority",
        "role_domain", "linkedin_job_url", "external_apply_url",
        "verified_badge", "posted_minutes_ago", "notes"
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(jobs)

def scrape_all_jobs(keywords_list: list = None, out_path: str = "all_scraped_jobs.csv"):
    if keywords_list is None:
        keywords_list = DEFAULT_KEYWORDS

    global FETCH_FAILURES, INCOMPLETE
    all_jobs = []
    seen_ids = set()

    def run(kw, offset):
        kw_jobs, failed = scrape_jobs_for_keyword(keyword=kw, seen_ids=seen_ids, start_offset=offset,
                                                  return_failed_offset=True)
        all_jobs.extend(kw_jobs)
        # Only checkpoint once something was collected, so a blocked run never
        # replaces an earlier good file with an empty one.
        if all_jobs:
            save_all_scraped(all_jobs, out_path)
        return failed

    pending = []  # (keyword, offset) still to finish
    for i, kw in enumerate(keywords_list, 1):
        failed = run(kw, 0)
        if failed is not None:
            pending.append((kw, failed))
        print(f"[{i}/{len(keywords_list)}] Total collected so far: {len(all_jobs)} jobs (saved to {out_path})", flush=True)
        _sleep(_pace)

    # Resume passes: after a cool-down, continue each throttled keyword from the page it stopped at.
    for p in range(1, RESUME_PASSES + 1):
        if not pending:
            break
        cooldown = RESUME_COOLDOWN * p
        print(f"\n=== Resume pass {p}/{RESUME_PASSES}: {len(pending)} keyword(s) left; cooling down {cooldown:.0f}s ===",
              flush=True)
        _sleep(cooldown)
        still = []
        for kw, offset in pending:
            failed = run(kw, offset)
            if failed is not None:
                still.append((kw, failed))
            print(f"[resume {p}] '{kw}' -> total {len(all_jobs)} jobs", flush=True)
        pending = still

    INCOMPLETE = [{"keyword": kw, "offset": off} for kw, off in pending]
    FETCH_FAILURES = len(pending)
    if pending:
        print(f"WARNING: {len(pending)} keyword(s) still incomplete after {RESUME_PASSES} resume passes: "
              + ", ".join(f"{kw}@{off}" for kw, off in pending), flush=True)
    print(f"[scrape] HTTP 429 responses seen: {THROTTLE_HITS}", flush=True)

    if not all_jobs and not FETCH_FAILURES:
        # Genuinely no matching jobs today: write a header-only file for downstream steps.
        save_all_scraped(all_jobs, out_path)

    return all_jobs

if __name__ == "__main__":
    keywords = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_KEYWORDS
    all_jobs = scrape_all_jobs(keywords)
    print(f"\nSuccessfully collected {len(all_jobs)} total unique jobs!", flush=True)
    if not all_jobs and FETCH_FAILURES:
        print(f"ERROR: 0 jobs and {FETCH_FAILURES} page fetch failure(s) - LinkedIn may be blocking this IP.", flush=True)
        sys.exit(1)

