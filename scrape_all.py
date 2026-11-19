import sys
import os
import re
import csv
import time
import socket
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

# Number of LinkedIn pages that could not be fetched after retries (blocking / rate limits).
FETCH_FAILURES = 0


def classify_role_domain(title: str) -> str:
    for label, pattern in ROLE_DOMAIN_RULES:
        if re.search(pattern, title or "", re.IGNORECASE):
            return label
    return "Other"


def scrape_jobs_for_keyword(keyword: str, geoid: str = "103644278", f_tpr: str = "r86400", f_wt: str = "2", f_e: str = "4", seen_ids: set = None):
    if seen_ids is None:
        seen_ids = set()

    base_api = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    jobs = []
    start = 0

    print(f"\n--- Scraping jobs for keyword='{keyword}' (US, Remote, 24h, 5+ YOE [f_E={f_e}]) ---", flush=True)

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
        req = urllib.request.Request(api_url, headers={
            "User-Agent": ua,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        html = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8")
                break
            except Exception as exc:
                wait_sec = 2 * (attempt + 1)
                print(f"Attempt {attempt+1} failed at offset {start}: {exc}. Waiting {wait_sec}s...")
                time.sleep(wait_sec)

        if not html:
            global FETCH_FAILURES
            FETCH_FAILURES += 1
            print(f"Could not fetch offset {start} after retries. Stopping keyword.")
            break

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", class_="job-search-card")
        if not cards:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                print("End of search results reached for keyword.")
                break
            start += 10
            time.sleep(1)
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
        time.sleep(1.2)

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

    all_jobs = []
    seen_ids = set()
    for i, kw in enumerate(keywords_list, 1):
        kw_jobs = scrape_jobs_for_keyword(keyword=kw, seen_ids=seen_ids)
        all_jobs.extend(kw_jobs)
        # Only checkpoint once something was collected, so a blocked run never
        # replaces an earlier good file with an empty one.
        if all_jobs:
            save_all_scraped(all_jobs, out_path)
        print(f"[{i}/{len(keywords_list)}] Total collected so far: {len(all_jobs)} jobs (saved to {out_path})", flush=True)
        time.sleep(1.5)

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

