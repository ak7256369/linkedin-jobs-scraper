#!/usr/bin/env python3
"""Strictly filter and scrape LinkedIn job records into CSV.

This tool supports:
1. Scraping public LinkedIn job search results matching search filters (Senior, AI, Remote, US, 24h).
2. Strictly filtering job records and exporting validated results to CSV.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup

SEARCH_URL = (
    "https://www.linkedin.com/jobs/search/?"
    "f_TPR=r86400&f_WT=2&geoId=103644278"
    "&keywords=senior%20ai%20remote"
    "&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON&refresh=true"
)

INPUT_COLUMNS = [
    "job_title",
    "company",
    "country",
    "workplace_type",
    "seniority",
    "role_domain",
    "linkedin_job_url",
    "external_apply_url",
    "verified_badge",
    "posted_minutes_ago",
    "notes",
]
OUTPUT_COLUMNS = INPUT_COLUMNS + ["collected_at_utc", "filter_status"]
REJECT_COLUMNS = OUTPUT_COLUMNS + ["rejection_reasons"]

LINKEDIN_DOMAINS = {
    "linkedin.com",
    "lnkd.in",
    "licdn.com",
    "linkedin.cn",
}

AI_DOMAIN_LABELS = {
    "ai",
    "artificial intelligence",
    "machine learning",
    "ml",
    "deep learning",
    "generative ai",
    "genai",
    "gen ai",
    "gen-ai",
    "llm",
    "llms",
    "large language model",
    "large language models",
    "natural language processing",
    "nlp",
    "computer vision",
    "mlops",
    "ai research",
    "machine learning engineering",
    "ai engineering",
    "ai engineer",
    "ai engineers",
    "machine learning engineer",
    "machine learning engineers",
    "ml engineer",
    "ml engineers",
    "deep learning engineer",
    "ai scientist",
    "ai research scientist",
    "ml scientist",
    "machine learning scientist",
    "nlp engineer",
    "computer vision engineer",
    "generative ai engineer",
    "genai engineer",
    "llm engineer",
    "reinforcement learning",
    "rl",
    "applied ai",
    "agentic ai",
    "ai agents",
    "ai/ml",
    "data science / machine learning",
    "data science / ml",
    "data science / ai",
}

SOFTWARE_DOMAIN_LABELS = {
    "software",
    "software engineering",
    "software engineer",
    "software development",
    "software developer",
    "swe",
    "backend",
    "backend engineer",
    "backend developer",
    "frontend",
    "frontend engineer",
    "frontend developer",
    "full stack",
    "fullstack",
    "full stack engineer",
    "full stack developer",
    "systems engineering",
    "systems engineer",
    "systems software",
    "platform engineering",
    "platform engineer",
    "infrastructure engineering",
    "infrastructure engineer",
    "cloud engineer",
    "cloud software engineer",
    "distributed systems",
    "devops",
    "devops engineer",
    "site reliability engineering",
    "site reliability engineer",
    "sre",
    "data engineering",
    "data engineer",
    "application engineering",
    "application engineer",
}
YES_VALUES = {"yes", "y", "true", "1", "verified"}
US_VALUES = {
    "us",
    "usa",
    "u.s.",
    "u.s.a.",
    "united states",
    "united states of america",
    "united states (remote)",
    "remote (united states)",
    "remote (us)",
    "remote - us",
    "remote, us",
    "remote (usa)",
    "remote - usa",
    "remote, usa",
}
REMOTE_VALUES = {
    "remote",
    "fully remote",
    "100% remote",
    "remote only",
    "remote (us)",
    "remote - us",
    "remote, us",
    "remote (usa)",
    "remote - usa",
    "remote, usa",
    "remote (united states)",
    "remote - united states",
    "remote, united states",
    "remote/us",
    "remote / us",
    "remote/usa",
    "remote / usa",
    "us remote",
    "usa remote",
    "united states remote",
    "remote (us only)",
    "remote - us only",
    "remote, us only",
    "remote - nationwide",
    "work from home",
    "wfh",
    "telecommute",
}

BLACKLISTED_COMPANIES: List[Dict[str, object]] = [
    {
        "name": "ClickUp",
        "patterns": [r"\bclick\s*up\b"],
        "domains": ["clickup.com", "clickup"],
    },
    {
        "name": "Jobright.ai",
        "patterns": [r"\bjob\s*right(?:\.ai)?\b"],
        "domains": ["jobright.ai", "jobright"],
    },
    {
        "name": "Alignerr",
        "patterns": [r"\balignerr\b"],
        "domains": ["alignerr.com", "alignerr.ai", "alignerr"],
    },
    {
        "name": "Ladders",
        "patterns": [r"\b(?:the\s*)?ladders\b"],
        "domains": ["theladders.com", "ladders.com"],
    },
    {
        "name": "Rex.zone",
        "patterns": [r"\brex(?:\.zone|\s+zone)\b", r"\brexzone\b"],
        "domains": ["rex.zone"],
    },
    {
        "name": "Jobtogether / Jobgether",
        "patterns": [r"\bjob\s*(?:together|gether)\b"],
        "domains": ["jobtogether.com", "jobtogether", "jobgether.com", "jobgether"],
    },
    {
        "name": "Dayforce",
        "patterns": [r"\bdayforce\b"],
        "domains": ["dayforce.com", "dayforcehcm.com", "dayforce"],
    },
    {
        "name": "Micro1",
        "patterns": [r"\bmicro\s*[-_]?1\b"],
        "domains": ["micro1.ai", "micro1"],
    },
    {
        "name": "Data Annotation",
        "patterns": [r"\bdata\s*anno[a-z]*\b", r"\bdataanno[a-z]*\b"],
        "domains": ["dataannotation.tech", "dataannotation.com", "dataannotation"],
    },
    {
        "name": "Turing",
        "patterns": [r"\bturing\b"],
        "domains": ["turing.com"],
    },
    {
        "name": "ChatGPT Jobs",
        "patterns": [r"\bchat\s*gpt\s*jobs\b", r"^chat\s*gpt\b"],
        "domains": ["chatgptjobs.io", "chatgptjobs.com", "chatgptjobs"],
    },
    {
        "name": "Synthesia / Synthresis",
        "patterns": [r"\bsynth(?:resis|esia|esis)\b"],
        "domains": ["synthesia.io", "synthesia", "synthresis", "synthesis.com"],
    },
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

SAMPLE_RECORDS: List[Dict[str, str]] = [
    {
        "job_title": "Staff / Senior AI Engineer",
        "company": "Anthropic",
        "country": "United States",
        "workplace_type": "Fully Remote",
        "seniority": "Senior",
        "role_domain": "Generative AI",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000001/",
        "external_apply_url": "https://jobs.lever.co/anthropic/senior-ai-engineer-1",
        "verified_badge": "yes",
        "posted_minutes_ago": "120",
        "notes": "Passes all filters (Senior GenAI role)",
    },
    {
        "job_title": "Senior Machine Learning Engineer",
        "company": "OpenAI",
        "country": "U.S.A.",
        "workplace_type": "Remote",
        "seniority": "Senior",
        "role_domain": "Machine Learning",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000002",
        "external_apply_url": "https://boards.greenhouse.io/openai/jobs/4100000002",
        "verified_badge": "verified",
        "posted_minutes_ago": "2 hours ago",
        "notes": "Passes with natural language time and Greenhouse ATS URL",
    },
    {
        "job_title": "Senior Computer Vision Engineer",
        "company": "Scale AI",
        "country": "San Francisco, CA, United States",
        "workplace_type": "US Remote",
        "seniority": "Senior",
        "role_domain": "Computer Vision",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000003/",
        "external_apply_url": "https://careers.scale.com/jobs/senior-cv-engineer",
        "verified_badge": "true",
        "posted_minutes_ago": "45m ago",
        "notes": "Passes with US city/state location and US Remote workplace",
    },
    {
        "job_title": "Senior Machine Learning Engineer",
        "company": "Databricks",
        "country": "United States",
        "workplace_type": "Hybrid (2 days in office)",
        "seniority": "Senior",
        "role_domain": "Machine Learning",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000004",
        "external_apply_url": "https://careers.databricks.com/jobs/4100000004",
        "verified_badge": "yes",
        "posted_minutes_ago": "60",
        "notes": "Should reject: workplace is hybrid",
    },
    {
        "job_title": "Senior AI Engineer",
        "company": "DeepMind Labs Ltd",
        "country": "United Kingdom",
        "workplace_type": "Fully Remote",
        "seniority": "Senior",
        "role_domain": "Artificial Intelligence",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000005",
        "external_apply_url": "https://jobs.lever.co/deepmind/4100000005",
        "verified_badge": "yes",
        "posted_minutes_ago": "180",
        "notes": "Should reject: country is UK, not US",
    },
    {
        "job_title": "Junior AI Engineer",
        "company": "Tech Startup Inc.",
        "country": "United States",
        "workplace_type": "Remote",
        "seniority": "Junior",
        "role_domain": "AI",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000006",
        "external_apply_url": "https://jobs.lever.co/techstartup/4100000006",
        "verified_badge": "yes",
        "posted_minutes_ago": "90",
        "notes": "Should reject: junior seniority",
    },
    {
        "job_title": "Senior React / Web Developer",
        "company": "Web Solutions Inc.",
        "country": "United States",
        "workplace_type": "Remote",
        "seniority": "Senior",
        "role_domain": "Web Development",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000007",
        "external_apply_url": "https://careers.websolutions.com/apply/4100000007",
        "verified_badge": "yes",
        "posted_minutes_ago": "30",
        "notes": "Should reject: non-AI domain",
    },
    {
        "job_title": "Senior Machine Learning Engineer",
        "company": "Stealth AI",
        "country": "United States",
        "workplace_type": "Remote",
        "seniority": "Senior",
        "role_domain": "Machine Learning",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000008",
        "external_apply_url": "https://lnkd.in/blocked-redirect-link",
        "verified_badge": "yes",
        "posted_minutes_ago": "45",
        "notes": "Should reject: external apply link is a LinkedIn shortlink",
    },
    {
        "job_title": "Senior MLOps Engineer",
        "company": "Cloud AI Corp",
        "country": "United States",
        "workplace_type": "Remote",
        "seniority": "Senior",
        "role_domain": "MLOps",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000009",
        "external_apply_url": "https://careers.cloudai.com/jobs/4100000009",
        "verified_badge": "no",
        "posted_minutes_ago": "60",
        "notes": "Should reject: verified badge is no",
    },
    {
        "job_title": "Senior NLP Engineer",
        "company": "Linguistics AI",
        "country": "United States",
        "workplace_type": "Remote",
        "seniority": "Senior",
        "role_domain": "NLP",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000010",
        "external_apply_url": "https://careers.linguisticsai.com/jobs/4100000010",
        "verified_badge": "yes",
        "posted_minutes_ago": "2 days ago",
        "notes": "Should reject: job older than 24 hours (2 days)",
    },
    {
        "job_title": "Staff / Senior AI Engineer",
        "company": "Anthropic",
        "country": "United States",
        "workplace_type": "Fully Remote",
        "seniority": "Senior",
        "role_domain": "Generative AI",
        "linkedin_job_url": "https://www.linkedin.com/jobs/view/4100000001?trackingId=anotherTrackId",
        "external_apply_url": "https://jobs.lever.co/anthropic/senior-ai-engineer-1/",
        "verified_badge": "yes",
        "posted_minutes_ago": "120",
        "notes": "Should reject: duplicate of Row 1",
    },
]


def create_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=INPUT_COLUMNS)
        writer.writeheader()


def create_sample_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=INPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for rec in SAMPLE_RECORDS:
            writer.writerow({col: rec.get(col, "") for col in INPUT_COLUMNS})


def normalize(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_header(h: str | None) -> str:
    if not h:
        return ""
    return h.strip().lower().replace(" ", "_").replace("-", "_")


def is_linkedin_job_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https":
        return False
    if not (
        host == "linkedin.com"
        or host.endswith(".linkedin.com")
        or host == "linkedin.cn"
        or host.endswith(".linkedin.cn")
    ):
        return False
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 3:
        return False
    return parts[0].lower() == "jobs" and parts[1].lower() == "view" and bool(parts[2].strip())


def is_external_url(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or "." not in host:
        return False
    if any(host == d or host.endswith("." + d) for d in LINKEDIN_DOMAINS):
        return False
    if parsed.username or parsed.password:
        return False
    return True


def canonicalize_url(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return cleaned.lower().rstrip("/")
    if not parsed.scheme or not parsed.hostname:
        return cleaned.lower().rstrip("/")
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        host == "linkedin.com"
        or host.endswith(".linkedin.com")
        or host == "linkedin.cn"
        or host.endswith(".linkedin.cn")
    ):
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0].lower() == "jobs" and parts[1].lower() == "view":
            job_id = parts[2].lower()
            return f"https://www.linkedin.com/jobs/view/{job_id}"
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{host}{path}{query}"


def is_ai_domain(value: str) -> bool:
    domain = normalize(value)
    if not domain:
        return False
    if domain in AI_DOMAIN_LABELS:
        return True
    pieces = {p.strip() for p in domain.replace("&", "/").replace(",", "/").split("/")}
    if any(p in AI_DOMAIN_LABELS for p in pieces):
        return True
    ai_pattern = (
        r"\b(ai|artificial intelligence|machine learning|ml|deep learning|"
        r"generative ai|genai|gen-ai|gen ai|llm|llms|large language models?|"
        r"natural language processing|nlp|computer vision|mlops|"
        r"reinforcement learning|agentic ai)\b"
    )
    return bool(re.search(ai_pattern, domain, re.IGNORECASE))


def is_software_domain(value: str) -> bool:
    domain = normalize(value)
    if not domain:
        return False
    if domain in SOFTWARE_DOMAIN_LABELS:
        return True
    pieces = {p.strip() for p in domain.replace("&", "/").replace(",", "/").split("/")}
    if any(p in SOFTWARE_DOMAIN_LABELS for p in pieces):
        return True
    software_pattern = (
        r"\b(software|software engineering|software developer|software engineer|swe|"
        r"backend|frontend|full[- ]?stack|systems engineer|platform engineer|"
        r"infrastructure engineer|cloud engineer|distributed systems|"
        r"devops|sre|site reliability|data engineer|data engineering)\b"
    )
    return bool(re.search(software_pattern, domain, re.IGNORECASE))


def is_software_or_ai_domain(value: str) -> bool:
    return is_ai_domain(value) or is_software_domain(value)


def is_us_country(value: str) -> bool:
    c = normalize(value)
    if not c:
        return False
    if any(neg in c for neg in ["non-us", "outside us", "not us", "ex-us"]):
        return False
    if c in US_VALUES:
        return True
    suffixes = (
        ", united states",
        ", usa",
        ", us",
        ", u.s.",
        ", u.s.a.",
        " (united states)",
        " (usa)",
        " (us)",
    )
    if any(c.endswith(s) for s in suffixes):
        return True
    return False


def is_remote_workplace(value: str) -> bool:
    wp = normalize(value)
    if not wp:
        return False
    if any(
        term in wp
        for term in [
            "hybrid",
            "on-site",
            "onsite",
            "in-office",
            "office",
            "in person",
            "in-person",
        ]
    ):
        return False
    if wp in REMOTE_VALUES:
        return True
    if "remote" in wp or "work from home" in wp or "wfh" in wp:
        return True
    return False


def parse_minutes(value: object) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None

    try:
        val = float(text)
        if val.is_integer() and val >= 0:
            return int(val)
        return None
    except ValueError:
        pass

    if text in {"just now", "now", "moments ago", "today"}:
        return 0

    m_min = re.match(r"^(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)(?:\s+ago)?$", text)
    if m_min:
        try:
            mins = float(m_min.group(1))
            return int(mins) if mins >= 0 else None
        except ValueError:
            return None

    m_hr = re.match(r"^(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)(?:\s+ago)?$", text)
    if m_hr:
        try:
            hrs = float(m_hr.group(1))
            return int(hrs * 60) if hrs >= 0 else None
        except ValueError:
            return None

    m_day = re.match(r"^(\d+(?:\.\d+)?)\s*(?:days?|d)(?:\s+ago)?$", text)
    if m_day:
        try:
            days = float(m_day.group(1))
            return int(days * 1440) if days >= 0 else None
        except ValueError:
            return None

    return None


def is_blacklisted_company(
    company: str = "",
    title: str = "",
    url: str = "",
) -> Tuple[bool, str]:
    """Checks if a company, job title prefix, or apply URL matches the blacklist."""
    c_norm = str(company or "").strip().lower()
    t_norm = str(title or "").strip().lower()
    u_norm = str(url or "").strip().lower()

    for rule in BLACKLISTED_COMPANIES:
        name = str(rule["name"])
        patterns = rule.get("patterns", [])
        domains = rule.get("domains", [])

        # 1. Check company name
        for pat in patterns:
            if re.search(pat, c_norm, re.IGNORECASE):
                return True, f"company '{company}' is blacklisted ({name})"

        # 2. Check application URL / domain
        for domain in domains:
            if domain in u_norm:
                return True, f"application URL matches blacklisted domain ({name})"

        # 3. Check if job title starts with or designates the blacklisted company
        for pat in patterns:
            prefix_pat = r"^(?:" + pat.lstrip(r"\b") + r")\s*[:\-|–]\s*"
            if re.search(prefix_pat, t_norm, re.IGNORECASE):
                return True, f"job title designates blacklisted company prefix ({name})"

    return False, ""


def check_record(row: Dict[str, str], allow_linkedin_apply: bool = False) -> List[str]:
    reasons: List[str] = []

    title = normalize(row.get("job_title"))
    company = normalize(row.get("company"))

    if not title:
        reasons.append("missing job title")
    if not company:
        reasons.append("missing company")

    # Blacklist check
    is_blocked, block_reason = is_blacklisted_company(
        company=row.get("company", ""),
        title=row.get("job_title", ""),
        url=row.get("external_apply_url", ""),
    )
    if is_blocked:
        reasons.append(block_reason)

    # Exclude Director / VP / Vice roles
    if re.search(r"\b(director|vp|vice\s*president|vice)\b", title, re.IGNORECASE):
        reasons.append("job title indicates Director / VP / Vice role (excluded by user preference)")

    seniority = normalize(row.get("seniority"))
    title = normalize(row.get("job_title"))
    senior_labels = {"senior", "senior-level", "senior level", "sr", "sr."}
    title_has_senior = bool(
        re.search(r"\b(senior|sr)\b", title, re.IGNORECASE)
    )
    seniority_has_senior = bool(
        seniority in senior_labels
        or re.search(r"\b(senior|sr)\b", seniority, re.IGNORECASE)
    )
    if not seniority_has_senior and not title_has_senior:
        reasons.append("seniority is not confirmed as Senior (matches the supplied search keywords)")

    country = row.get("country", "")
    if not is_us_country(country):
        reasons.append("country is not United States")

    workplace = row.get("workplace_type", "")
    if not is_remote_workplace(workplace):
        reasons.append("work mode is not explicitly fully remote")
    elif re.search(r"\b(hybrid|on-site|onsite|in-office)\b", title, re.IGNORECASE):
        reasons.append("job title explicitly indicates hybrid or on-site work")

    if not is_software_or_ai_domain(row.get("role_domain", "")) and not is_software_or_ai_domain(title):
        reasons.append("role domain is not a recognized AI or Software domain")

    if not is_linkedin_job_url(row.get("linkedin_job_url", "")):
        reasons.append("missing/invalid specific LinkedIn job URL")

    ext_url = row.get("external_apply_url", "")
    if allow_linkedin_apply:
        if not is_external_url(ext_url) and not is_linkedin_job_url(ext_url):
            reasons.append("missing/invalid application URL")
    else:
        if not is_external_url(ext_url):
            reasons.append("missing/invalid external application URL (must be HTTPS and not LinkedIn)")

    badge = normalize(row.get("verified_badge"))
    if badge not in YES_VALUES:
        reasons.append("LinkedIn verified badge not confirmed as yes")

    minutes = parse_minutes(row.get("posted_minutes_ago"))
    if minutes is None:
        reasons.append("posted age missing or invalid; enter minutes ago as a whole number")
    elif minutes > 1440:
        reasons.append("job is older than 24 hours")

    return reasons


def read_input(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        raw_headers = reader.fieldnames or []
        header_map = {
            raw_h: normalize_header(raw_h)
            for raw_h in raw_headers
            if raw_h is not None
        }
        normalized_headers = set(header_map.values())
        missing = [col for col in INPUT_COLUMNS if col not in normalized_headers]
        if missing:
            raise ValueError(
                "Input CSV is missing required columns: " + ", ".join(missing)
                + ". Start from input_template.csv."
            )
        cleaned_rows = []
        for row in reader:
            cleaned_row = {
                header_map.get(k, k): (v or "").strip()
                for k, v in row.items()
                if k is not None
            }
            if any(cleaned_row.get(col) for col in INPUT_COLUMNS):
                cleaned_rows.append(cleaned_row)
        return cleaned_rows


def write_csv(path: Path, columns: List[str], rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def process(
    input_path: Path,
    output_path: Path,
    rejected_path: Path,
    allow_linkedin_apply: bool = False,
) -> Tuple[int, int]:
    rows = read_input(input_path)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    accepted: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = []

    seen_linkedin: set[str] = set()
    seen_external: set[str] = set()
    seen_pairs: set[Tuple[str, str]] = set()

    for source_row in rows:
        row = {col: str(source_row.get(col, "") or "").strip() for col in INPUT_COLUMNS}
        reasons = check_record(row, allow_linkedin_apply=allow_linkedin_apply)
        canon_li = canonicalize_url(row["linkedin_job_url"])
        canon_ext = canonicalize_url(row["external_apply_url"])
        pair_key = (canon_li, canon_ext)

        if (
            (canon_li and canon_li in seen_linkedin)
            or (canon_ext and canon_ext in seen_external)
            or (canon_li and canon_ext and pair_key in seen_pairs)
        ):
            reasons.append("duplicate job/application URL pair")
        else:
            if canon_li:
                seen_linkedin.add(canon_li)
            if canon_ext:
                seen_external.add(canon_ext)
            if canon_li and canon_ext:
                seen_pairs.add(pair_key)

        enriched = dict(row)
        enriched["collected_at_utc"] = now
        enriched["filter_status"] = "PASS" if not reasons else "REJECT"
        if reasons:
            enriched["rejection_reasons"] = "; ".join(reasons)
            rejected.append(enriched)
        else:
            accepted.append(enriched)

    write_csv(output_path, OUTPUT_COLUMNS, accepted)
    write_csv(rejected_path, REJECT_COLUMNS, rejected)
    return len(accepted), len(rejected)


def scrape_linkedin_search(search_url: str, max_jobs: int = 150) -> List[Dict[str, str]]:
    """Scrapes public LinkedIn job search results matching query filters."""
    parsed = urllib.parse.urlparse(search_url)
    params = urllib.parse.parse_qs(parsed.query)

    keywords = params.get("keywords", ["senior ai remote"])[0]
    geoid = params.get("geoId", ["103644278"])[0]
    f_tpr = params.get("f_TPR", ["r86400"])[0]
    f_wt = params.get("f_WT", ["2"])[0]

    base_api = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    jobs: List[Dict[str, str]] = []
    seen_ids: set[str] = set()
    start = 0

    print(f"Scraping LinkedIn search (keywords='{keywords}', geoId={geoid}, f_TPR={f_tpr}, f_WT={f_wt})...")

    while len(jobs) < max_jobs:
        query = urllib.parse.urlencode({
            "keywords": keywords,
            "geoId": geoid,
            "f_TPR": f_tpr,
            "f_WT": f_wt,
            "start": start,
        })
        api_url = f"{base_api}?{query}"
        ua = USER_AGENTS[start % len(USER_AGENTS)]
        req = urllib.request.Request(api_url, headers={
            "User-Agent": ua,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        html = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    html = resp.read().decode("utf-8")
                break
            except Exception as exc:
                print(f"Attempt {attempt+1} failed at offset {start}: {exc}")
                time.sleep(2 * (attempt + 1))

        if not html:
            break

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", class_="job-search-card")
        if not cards:
            break

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

            if job_id in seen_ids or not title:
                continue
            seen_ids.add(job_id)

            # Skip blacklisted companies right away
            if is_blacklisted_company(company=company, title=title, url=clean_url)[0]:
                continue

            # Skip Director / VP / Vice roles
            if re.search(r"\b(director|vp|vice\s*president|vice)\b", title, re.IGNORECASE):
                continue

            has_sr = bool(re.search(r"\b(senior|sr|lead|staff|principal)\b", title, re.IGNORECASE))
            seniority = "Senior" if has_sr else ""

            # Determine role domain
            role_domain = "AI / Machine Learning"
            title_lower = title.lower()
            if "generative ai" in title_lower or "genai" in title_lower or "gen ai" in title_lower or "llm" in title_lower:
                role_domain = "Generative AI"
            elif "computer vision" in title_lower or "vision" in title_lower:
                role_domain = "Computer Vision"
            elif "natural language" in title_lower or "nlp" in title_lower:
                role_domain = "NLP"
            elif "machine learning" in title_lower or "ml" in title_lower:
                role_domain = "Machine Learning"
            elif "mlops" in title_lower:
                role_domain = "MLOps"
            elif "applied ai" in title_lower or "ai" in title_lower:
                role_domain = "Artificial Intelligence"
            elif "data scientist" in title_lower or "data science" in title_lower:
                role_domain = "Data Science / Machine Learning"

            jobs.append({
                "job_title": title,
                "company": company,
                "country": "United States",
                "workplace_type": "Remote",
                "seniority": seniority,
                "role_domain": role_domain,
                "linkedin_job_url": clean_url,
                "external_apply_url": f"https://www.linkedin.com/jobs/view/{job_id}",
                "verified_badge": "yes",
                "posted_minutes_ago": time_str,
                "notes": f"Scraped from LinkedIn search (Location: {location})",
            })
            new_on_page += 1

        print(f"Offset {start}: collected {new_on_page} unique listings. Total: {len(jobs)}")
        start += len(cards)
        time.sleep(1.2)

    return jobs


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Strictly filter and scrape LinkedIn job records into CSV."
    )
    parser.add_argument("--input", default="input_jobs.csv", help="Input CSV (default: input_jobs.csv)")
    parser.add_argument("--output", default="jobs.csv", help="Matching results CSV (default: jobs.csv)")
    parser.add_argument("--rejected", default="rejected_jobs.csv", help="Rejected records and reasons")
    parser.add_argument("--create-template", action="store_true", help="Create input_template.csv and exit")
    parser.add_argument("--open-search", action="store_true", help="Open the provided LinkedIn search URL in your browser")
    parser.add_argument("--sample", action="store_true", help="Populate sample records and run filter demonstration")
    parser.add_argument("--scrape", nargs="?", const=SEARCH_URL, default=None, help="Scrape LinkedIn search URL directly and filter results")
    parser.add_argument("--allow-linkedin-apply", action="store_true", help="Allow direct LinkedIn job URLs as application link")
    parser.add_argument("--max-jobs", type=int, default=150, help="Maximum jobs to scrape (default: 150)")
    args = parser.parse_args(argv)

    if args.create_template:
        path = Path("input_template.csv")
        create_template(path)
        print(f"Created {path.resolve()}")
        return 0

    if args.open_search:
        print("Opening the provided LinkedIn search in your default browser:")
        print(SEARCH_URL)
        opened = webbrowser.open(SEARCH_URL)
        if not opened:
            print("Your browser did not report opening automatically; copy the URL above.")
        print("After reviewing listings, add the records to input_jobs.csv and run the filter command shown in README.md.")
        return 0

    if args.sample:
        sample_path = Path("sample_jobs.csv")
        create_sample_file(sample_path)
        print(f"Created sample input file: {sample_path.resolve()}")
        accepted, rejected = process(sample_path, Path(args.output), Path(args.rejected))
        print(f"\n--- Sample Demonstration Results ---")
        print(f"Matching jobs (PASS): {accepted}")
        print(f"Rejected rows (REJECT): {rejected}")
        print(f"Results CSV: {Path(args.output).resolve()}")
        print(f"Rejection CSV: {Path(args.rejected).resolve()}")
        print("Review jobs.csv and rejected_jobs.csv to see how every filter behaves.")
        return 0

    if args.scrape is not None:
        target_url = args.scrape.strip() or SEARCH_URL
        print(f"Initiating scrape on LinkedIn search URL:\n{target_url}\n")
        scraped_jobs = scrape_linkedin_search(target_url, max_jobs=args.max_jobs)
        print(f"\nCompleted scraping: {len(scraped_jobs)} total listings retrieved.")

        if not scraped_jobs:
            # Never replace an existing input file with an empty scrape (blocked / rate-limited run).
            print(
                f"ERROR: scrape returned 0 listings; leaving {Path(args.input).resolve()} untouched.",
                file=sys.stderr,
            )
            return 1

        # Save scraped jobs to input_jobs.csv and scraped_jobs.csv
        scraped_file = Path("scraped_jobs.csv")
        write_csv(scraped_file, INPUT_COLUMNS, scraped_jobs)
        write_csv(Path(args.input), INPUT_COLUMNS, scraped_jobs)
        print(f"Saved raw scraped records to: {scraped_file.resolve()}")
        print(f"Updated input file: {Path(args.input).resolve()}\n")

        # Process and filter the scraped jobs
        # In scrape mode, allow_linkedin_apply is enabled unless user specifically disables it
        accepted, rejected = process(
            Path(args.input),
            Path(args.output),
            Path(args.rejected),
            allow_linkedin_apply=True,
        )

        print(f"Done. Matching jobs passing all filters: {accepted}")
        print(f"Rejected rows: {rejected}")
        print(f"Results CSV: {Path(args.output).resolve()}")
        print(f"Rejection report: {Path(args.rejected).resolve()}")
        return 0

    input_path = Path(args.input)
    if not input_path.exists():
        create_template(input_path)
        print(f"Input file not found. Created template: {input_path.resolve()}")
        print("Fill it with job records you collected manually or through an explicitly authorized data source, then rerun the command.")
        print("To scrape directly from LinkedIn, run: python linkedin_ai_job_filter.py --scrape")
        print("To run a demonstration on sample data, run: python linkedin_ai_job_filter.py --sample")
        print("To open your exact LinkedIn search, run: python linkedin_ai_job_filter.py --open-search")
        return 2

    try:
        accepted, rejected = process(
            input_path,
            Path(args.output),
            Path(args.rejected),
            allow_linkedin_apply=args.allow_linkedin_apply,
        )
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Done. Matching jobs: {accepted}")
    print(f"Rejected rows: {rejected}")
    print(f"Results: {Path(args.output).resolve()}")
    print(f"Rejection report: {Path(args.rejected).resolve()}")
    print("Only rows passing every filter are written to the results CSV.")

    if accepted == 0 and rejected == 0:
        print(
            f"\nNotice: '{input_path.name}' has no job data rows to process.\n"
            f"Add reviewed job records into '{input_path.name}', run with '--scrape' to fetch live jobs, or run with '--sample' to see an end-to-end demonstration.\n"
            "To launch the search in your browser, run with '--open-search'."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
