"""Resolve LinkedIn jobs (all_scraped_jobs.csv) to exact external career URLs and cross-verify all filters.

Resolution tiers (no LinkedIn login required, zero LinkedIn URLs in outputs):
1. Public ATS board APIs: Greenhouse (US/EU), Ashby, Lever (US/EU), SmartRecruiters,
   Rippling, Gem, Workable, BreezyHR, Recruitee, Dover, Pinpoint.
2. Enterprise & Workday portals: Workday CXS (auto-discovered + known tenants),
   Oracle Cloud HCM, IBM Careers, Atlassian, Amazon Jobs, GitHub Careers (Jibe),
   Avature, TalentBrew/Radancy, Eightfold.
3. Automatic company career website discovery: looks up official domain and probes
   /careers /jobs pages to discover embedded ATS links or direct job postings.

Every resolved external posting is then cross-verified against all filters
(external non-LinkedIn HTTPS URL, non-aggregator, non-blacklisted, non-Director/VP,
Senior seniority, AI/Software domain, US country, and confirmed Remote work mode).

Usage: py resolve_ats_links.py [input_csv] [output_csv]
"""
import csv
import html as htmlmod
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from bs4 import BeautifulSoup

from audit_external_links import AGGREGATOR_DOMAINS
from linkedin_ai_job_filter import (
    canonicalize_url,
    is_blacklisted_company,
    is_external_url,
    is_software_or_ai_domain,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
IN_CSV = sys.argv[1] if len(sys.argv) > 1 else "all_scraped_jobs.csv"
OUT_CSV = sys.argv[2] if len(sys.argv) > 2 else "external_links_24h.csv"

NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montreal|calgary|ottawa|waterloo|edmonton|united kingdom|\buk\b|london|manchester|"
    r"edinburgh|bristol|belfast|india|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|chennai|noida|gurgaon|"
    r"germany|berlin|munich|hamburg|frankfurt|france|paris|lyon|australia|sydney|melbourne|brisbane|perth|"
    r"new zealand|auckland|poland|warsaw|krakow|brazil|sao paulo|spain|barcelona|madrid|portugal|lisbon|"
    r"mexico|mexico city|guadalajara|ireland|dublin|netherlands|amsterdam|israel|tel aviv|singapore|"
    r"japan|tokyo|korea|seoul|taiwan|taipei|argentina|buenos aires|colombia|bogota|medellin|chile|santiago|"
    r"peru|lima|costa rica|romania|bucharest|philippines|manila|malaysia|indonesia|vietnam|thailand|"
    r"norway|oslo|denmark|copenhagen|finland|helsinki|belgium|brussels|austria|vienna|czech|prague|"
    r"hungary|budapest|greece|athens|turkey|istanbul|emea|apac|latam|europe|china|shanghai|beijing|"
    r"sweden|stockholm|switzerland|zurich|geneva|italy|milan|rome|south africa|cape town)\b",
    re.I,
)
US_HINT = re.compile(
    r"\b(us|usa|u\.s\.?|united states|america|useast|uswest|us-east|us-west|nationwide|"
    r"san francisco|new york|nyc|austin|seattle|boston|chicago|denver|los angeles|atlanta|dallas|houston|miami|"
    r"washington|portland|raleigh|san diego|san jose|philadelphia|pittsburgh|phoenix|charlotte|remote[- ]us)\b",
    re.I,
)
US_STATE = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
)
COUNTRY_ONLY = re.compile(
    r"^\s*(united states(?: of america)?|usa|us|north america|americas?)(?:\s*[;,|]\s*(united states(?: of america)?|usa|us|north america|latin america))*\s*$",
    re.I,
)
TECH_HYBRID = (
    r"(?!\s*[-/]?\s*(?:cloud|infrastructure|search|retrieval|numerical|models?|approach|architectures?|"
    r"environments?|deployments?|ml\b|systems?|vehicles?|quantum|rag|apps?|mobile|integration|data|on-?prem|"
    r"multi-cloud|workloads?|solutions?|methods?|ai\b|&\s*remote|/\s*remote|and\s+remote|or\s+remote))"
)
STRONG_DESC = [
    r"\b(?:this|the)\s+(?:role|position|job|opportunity)\s+(?:is|will\s+be|requires)\s+(?:a\s+|an?\s+)?(?:hybrid|on[- ]?site|in[- ]office|in[- ]person)",
    r"\bhybrid\s+(?:role|position)\s+(?:based|located|in|out\s+of)\b",
    r"\b(?:full[- ]time,?\s+)?hybrid\s+position\b",
    r"\b\d\s*(?:\+|-\s*\d)?\s*days?\s*(?:a|per|each)\s*week\s*(?:in|at|on)[- ](?:the\s*|our\s*)?(?:office|site|campus|hq)",
    r"\b(?:requires?|requiring)\s+\d\s*(?:-\s*\d)?\s*days?\s*(?:a|per)\s*week\s+in",
    r"\bmust\s+(?:be\s+able\s+to\s+)?(?:commute|report)\s+(?:daily\s+)?to\s+(?:our|the)\s+\w+\s*office",
    r"\bnot\s+(?:a\s+)?(?:fully\s+)?remote\s+(?:role|position)\b",
    r"#LI-(?:Hybrid|Onsite)\b(?![^#]{0,15}#LI-Remote)",
]
WEAK_DESC = STRONG_DESC + [
    r"\bhybrid\b" + TECH_HYBRID,
    r"\bin[- ]office\s+(?:role|position|requirement)",
    r"\bon[- ]?site\s+(?:role|position|only)\b",
]
SENIOR_RE = re.compile(r"\b(senior|sr|lead|staff|principal)\b", re.I)
DISALLOWED_TITLE_RE = re.compile(r"\b(director|vp|vice\s*president|vice)\b", re.I)
JUNIOR_RE = re.compile(r"\b(junior|jr\.?|intern|internship|entry[- ]level|new grad|associate|co-?op)\b", re.I)

COMPANY_ALIASES = {
    "cash app": ["block", "cashapp"],
    "bolt.new": ["stackblitz", "bolt"],
    "optum": ["optum", "unitedhealthgroup"],
    "hewlett packard enterprise": ["hpe", "hewlettpackardenterprise"],
    "general motors": ["generalmotors", "gm"],
    "cvs health": ["cvshealth", "cvs"],
    "state street": ["statestreet"],
    "american express": ["aexp", "americanexpress"],
    "jack henry": ["jackhenry"],
    "general dynamics information technology": ["gdit"],
}

DEFAULT_WORKDAY_TENANTS = {
    "nvidia": [("nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite")],
    "zillow": [("zillow.wd5.myworkdayjobs.com", "zillow", "Zillow_Group_External")],
    "pwc": [
        ("pwc.wd3.myworkdayjobs.com", "pwc", "Global_Experienced_Careers"),
        ("pwc.wd3.myworkdayjobs.com", "pwc", "US_Experienced_Careers"),
    ],
    "accenture": [("accenture.wd103.myworkdayjobs.com", "accenture", "AccentureCareers")],
    "salesforce": [("salesforce.wd12.myworkdayjobs.com", "salesforce", "External_Career_Site")],
    "workday": [("workday.wd5.myworkdayjobs.com", "workday", "Workday")],
    "statestreet": [("statestreet.wd1.myworkdayjobs.com", "statestreet", "Global")],
    "hpe": [("hpe.wd5.myworkdayjobs.com", "hpe", "Jobsathpe")],
    "proofpoint": [("proofpoint.wd5.myworkdayjobs.com", "proofpoint", "ProofpointCareers")],
    "generalmotors": [("generalmotors.wd5.myworkdayjobs.com", "generalmotors", "Careers_GM")],
    "genesys": [("genesys.wd1.myworkdayjobs.com", "genesys", "Genesys")],
    "cvshealth": [("cvshealth.wd1.myworkdayjobs.com", "cvshealth", "CVS_Health_Careers")],
    "autodesk": [("autodesk.wd1.myworkdayjobs.com", "autodesk", "Ext")],
    "humana": [("humana.wd5.myworkdayjobs.com", "humana", "Humana_External_Career_Site")],
    "paypal": [("paypal.wd1.myworkdayjobs.com", "paypal", "jobs")],
    "adobe": [("adobe.wd5.myworkdayjobs.com", "adobe", "external_experienced")],
    "mastercard": [("mastercard.wd1.myworkdayjobs.com", "mastercard", "CorporateCareers")],
}

OUTPUT_FIELDS = [
    "filter_status",
    "remote_status",
    "job_title",
    "company",
    "external_apply_url",
    "ats",
    "ats_title",
    "ats_location",
    "remote_evidence",
    "rejection_reasons",
    "match_score",
    "posted_minutes_ago",
    "role_domain",
]

_lock = threading.Lock()
_key_locks: dict = {}
_cache: dict = {}


# ---------------- HTTP & text helpers ----------------
def fetch(url, data=None, timeout=20, retries=2):
    """Fetch JSON from an endpoint, failing fast on 4xx and retrying transient errors."""
    headers = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"}
    payload = None
    if data is not None:
        payload = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 410, 422):
                return None
            if attempt >= retries:
                return None
        except Exception:
            if attempt >= retries:
                return None
        time.sleep(0.8 * (attempt + 1))
    return None


def fetch_html_page(url, timeout=8):
    """Fetch HTML text and final redirected URL from a web page."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype and "json" not in ctype:
                return r.geturl(), ""
            return r.geturl(), r.read(1_500_000).decode("utf-8", "ignore")
    except Exception:
        return url, ""


def cached(key, fn):
    """Memoize lookups using a per-key single-flight lock."""
    with _lock:
        if key in _cache:
            return _cache[key]
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        with _lock:
            if key in _cache:
                return _cache[key]
        val = fn()
        with _lock:
            _cache[key] = val
        return val


def strip_html(s):
    s = htmlmod.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


def norm_title(t):
    t = t.lower()
    t = re.sub(
        r"\b(remote eligible|fully remote|remote|us|usa|united states|u\.s\.|anywhere|full[- ]time|contract|pst|est|cst|mst)\b",
        " ",
        t,
    )
    t = re.sub(r"\bsr\b\.?", "senior", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_score(a, b):
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def slugs_for(company):
    c_raw = company.strip().lower()
    aliases = list(COMPANY_ALIASES.get(c_raw, []))
    c = c_raw.replace("&", "and")
    c = re.sub(r"[’'`]", "", c)
    c = re.sub(
        r"\b(inc|llc|ltd|corp|corporation|co|company|group|technologies|technology|labs|hq|the|plc|gmbh|ai)\b\.?",
        " ",
        c,
    )
    words = re.findall(r"[a-z0-9]+", c)
    out = aliases[:]
    if words:
        out += ["".join(words), "-".join(words), words[0]]
    full = re.findall(r"[a-z0-9]+", re.sub(r"[’'`]", "", c_raw))
    if full:
        out += ["".join(full), "-".join(full)]
    seen, res = set(), []
    for s in out:
        if s and s not in seen and len(s) > 1:
            seen.add(s)
            res.append(s)
    return res[:5]


def search_terms_for(title):
    """Extract clean keyword query for search-based career portals."""
    nt = norm_title(title)
    words = [w for w in nt.split() if len(w) > 2]
    return " ".join(words[:5]) or nt


# ---------------- Tier 1: Direct Public ATS Boards ----------------
def _gh_board_at(base, slug):
    d = fetch(f"{base}/v1/boards/{slug}/jobs", timeout=15)
    if not d or not d.get("jobs"):
        return None
    return [
        {
            "ats": "greenhouse",
            "api_base": base,
            "slug": slug,
            "id": j["id"],
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": re.sub(r"^http://", "https://", j.get("absolute_url", ""), flags=re.I),
        }
        for j in d["jobs"]
    ]


def gh_board(slug):
    return _gh_board_at("https://boards-api.greenhouse.io", slug) or _gh_board_at(
        "https://boards-api.eu.greenhouse.io", slug
    )


def gh_detail(p):
    base = p.get("api_base") or "https://boards-api.greenhouse.io"
    d = fetch(f"{base}/v1/boards/{p['slug']}/jobs/{p['id']}", timeout=15) or {}
    meta = " | ".join(f"{m.get('name')}: {m.get('value')}" for m in (d.get("metadata") or []) if m.get("value"))
    offices = ", ".join(o.get("name", "") for o in (d.get("offices") or []))
    return {
        "structured": "",
        "location": p.get("location", ""),
        "extra": f"{meta} {offices}".strip(),
        "desc": strip_html(d.get("content", "")),
    }


def _lever_board_at(base, slug):
    d = fetch(f"{base}/v0/postings/{slug}?mode=json", timeout=15)
    if not isinstance(d, list) or not d:
        return None
    return [
        {
            "ats": "lever",
            "slug": slug,
            "id": j["id"],
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", "")
            or ", ".join((j.get("categories") or {}).get("allLocations", []) or []),
            "url": j.get("hostedUrl", ""),
            "wpt": j.get("workplaceType", ""),
            "desc": strip_html(
                (j.get("descriptionPlain") or "")
                + " "
                + " ".join(strip_html(l.get("content", "")) for l in j.get("lists", []))
                + " "
                + (j.get("additionalPlain") or "")
            ),
        }
        for j in d
    ]


def lever_board(slug):
    return _lever_board_at("https://api.lever.co", slug) or _lever_board_at("https://api.eu.lever.co", slug)


def ashby_board(slug):
    d = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=15)
    if not d or not d.get("jobs"):
        return None
    out = []
    for j in d["jobs"]:
        locs = [j.get("location", "")] + [s.get("location", "") for s in (j.get("secondaryLocations") or [])]
        out.append(
            {
                "ats": "ashby",
                "slug": slug,
                "id": j.get("id"),
                "title": j.get("title", ""),
                "location": ", ".join(l for l in locs if l),
                "url": j.get("jobUrl", ""),
                "wpt": j.get("workplaceType") or ("Remote" if j.get("isRemote") else ""),
                "desc": j.get("descriptionPlain", ""),
            }
        )
    return out


def sr_board(slug):
    d = fetch(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100", timeout=15)
    if not d or not d.get("content"):
        return None
    out = []
    for j in d["content"]:
        loc = j.get("location") or {}
        wpt = "Remote" if loc.get("remote") else ("Hybrid" if loc.get("hybrid") else "Onsite")
        out.append(
            {
                "ats": "smartrecruiters",
                "slug": slug,
                "id": j.get("id"),
                "title": j.get("name", ""),
                "location": loc.get("fullLocation", "")
                or ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])),
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                "wpt": wpt,
            }
        )
    return out


def sr_detail(p):
    d = fetch(f"https://api.smartrecruiters.com/v1/companies/{p['slug']}/postings/{p['id']}", timeout=15) or {}
    secs = (d.get("jobAd") or {}).get("sections") or {}
    return " ".join(strip_html((v or {}).get("text", "")) for v in secs.values() if isinstance(v, dict))


def rippling_board(slug):
    d = fetch(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs", timeout=10, retries=0)
    if not isinstance(d, list) or not d:
        return None
    return [
        {
            "ats": "rippling",
            "slug": slug,
            "id": j.get("uuid"),
            "title": (j.get("name") or "").strip(),
            "location": ((j.get("workLocation") or {}).get("label") or "").strip(),
            "url": j.get("url", ""),
        }
        for j in d
        if j.get("url")
    ]


def gem_board(slug):
    d = fetch(f"https://api.gem.com/job_board/v0/{slug}/job_posts/", timeout=10, retries=0)
    if not isinstance(d, list) or not d:
        return None
    return [
        {
            "ats": "gem",
            "slug": slug,
            "id": j.get("id"),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": re.sub(r"^http://", "https://", j.get("absolute_url", ""), flags=re.I),
            "wpt": j.get("location_type", ""),
            "desc": j.get("content_plain", ""),
        }
        for j in d
        if j.get("absolute_url")
    ]


def workable_board(slug):
    d = fetch(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=10, retries=0)
    jobs = (d or {}).get("jobs") if isinstance(d, dict) else None
    if not jobs:
        return None
    out = []
    for j in jobs:
        loc = ", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))
        out.append(
            {
                "ats": "workable",
                "slug": slug,
                "id": j.get("shortcode"),
                "title": j.get("title", ""),
                "location": f"{loc} (Remote)" if j.get("telecommuting") and "remote" not in loc.lower() else loc,
                "url": j.get("url") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode')}/",
                "wpt": "Remote" if j.get("telecommuting") else "",
            }
        )
    return out


def dover_board(slug):
    d = fetch(f"https://app.dover.com/api/v1/careers-page/{slug}/jobs", timeout=8, retries=0)
    jobs = (d or {}).get("results") if isinstance(d, dict) else (d if isinstance(d, list) else None)
    if not jobs:
        return None
    out = []
    for j in jobs:
        jid = j.get("id") or j.get("job_id")
        if not jid:
            continue
        loc = j.get("locations") or j.get("location") or ""
        if isinstance(loc, list):
            loc = ", ".join(str(x) for x in loc)
        out.append(
            {
                "ats": "dover",
                "slug": slug,
                "id": jid,
                "title": j.get("title", ""),
                "location": str(loc),
                "url": j.get("url") or f"https://app.dover.com/apply/{slug}/{jid}",
                "wpt": "Remote" if j.get("is_remote") else "",
            }
        )
    return out


def pinpoint_board(slug):
    d = fetch(f"https://{slug}.pinpointhq.com/postings.json", timeout=8, retries=0)
    jobs = (d or {}).get("data") if isinstance(d, dict) else None
    if not jobs:
        return None
    return [
        {
            "ats": "pinpoint",
            "slug": slug,
            "id": j.get("id"),
            "title": j.get("title", ""),
            "location": ((j.get("location") or {}).get("name") or ""),
            "url": j.get("url", ""),
            "wpt": j.get("workplace_type", ""),
            "desc": strip_html(j.get("description", "")),
        }
        for j in jobs
        if j.get("url")
    ]


def recruitee_board(slug):
    d = fetch(f"https://{slug}.recruitee.com/api/offers/", timeout=8, retries=0)
    offers = (d or {}).get("offers") if isinstance(d, dict) else None
    if not offers:
        return None
    return [
        {
            "ats": "recruitee",
            "slug": slug,
            "id": j.get("id"),
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "url": j.get("careers_url", ""),
            "wpt": "Remote" if j.get("remote") else ("Hybrid" if j.get("hybrid") else ""),
            "desc": strip_html(j.get("description", "")),
        }
        for j in offers
        if j.get("careers_url")
    ]


def breezy_board(slug):
    d = fetch(f"https://{slug}.breezy.hr/json", timeout=8, retries=0)
    if not isinstance(d, list) or not d:
        return None
    return [
        {
            "ats": "breezy",
            "slug": slug,
            "id": j.get("id"),
            "title": j.get("name", ""),
            "location": ((j.get("location") or {}).get("name") or ""),
            "url": j.get("url", ""),
            "wpt": "Remote" if ((j.get("location") or {}).get("is_remote")) else "",
        }
        for j in d
        if j.get("url")
    ]


BOARD_FNS = [
    ("greenhouse", gh_board),
    ("ashby", ashby_board),
    ("lever", lever_board),
    ("smartrecruiters", sr_board),
    ("workable", workable_board),
    ("rippling", rippling_board),
    ("gem", gem_board),
]
EXTRA_BOARD_FNS = {
    "dover": dover_board,
    "pinpoint": pinpoint_board,
    "recruitee": recruitee_board,
    "breezy": breezy_board,
}


# ---------------- Tier 2: Enterprise Portals & Workday ----------------
def load_workday_tenants():
    tenants = {k: list(v) for k, v in DEFAULT_WORKDAY_TENANTS.items()}
    pat = re.compile(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/,\s\"'?#]+)")
    for path in ("external_links.csv", "external_links_24h.csv", "remote_external_links_24h.csv"):
        try:
            with open(path, encoding="utf-8-sig") as f:
                for line in f:
                    m = pat.search(line)
                    if m:
                        tenant, wd, site = m.groups()
                        entry = (f"{tenant}.{wd}.myworkdayjobs.com", tenant, site)
                        lst = tenants.setdefault(tenant.lower(), [])
                        if entry not in lst:
                            lst.append(entry)
        except FileNotFoundError:
            pass
    return tenants


WORKDAY_TENANTS = load_workday_tenants()


def workday_search_cfg(cfg, title):
    host, tenant, site = cfg
    q = search_terms_for(title)
    d = cached(
        ("wd_search", host, site, q),
        lambda: fetch(
            f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
            {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q},
            timeout=15,
        ),
    )
    if not d or not d.get("jobPostings"):
        return []
    return [
        {
            "ats": "workday",
            "slug": f"{host}|{tenant}|{site}",
            "id": j.get("externalPath"),
            "title": j.get("title", ""),
            "location": j.get("locationsText", ""),
            "url": f"https://{host}/en-US/{site}{j.get('externalPath', '')}",
            "remoteType": j.get("remoteType", ""),
        }
        for j in d["jobPostings"]
        if j.get("externalPath")
    ]


def workday_search(company, title, extra_cfgs=None):
    cfgs = list(extra_cfgs or [])
    for s in slugs_for(company):
        k = s.replace("-", "")
        cfgs.extend(WORKDAY_TENANTS.get(k, []))
    seen, unique_cfgs = set(), []
    for cfg in cfgs:
        if cfg not in seen:
            seen.add(cfg)
            unique_cfgs.append(cfg)
    results = []
    for cfg in unique_cfgs[:3]:
        results.extend(workday_search_cfg(cfg, title))
    return results


def workday_detail(p):
    host, tenant, site = p["slug"].split("|")
    d = fetch(f"https://{host}/wday/cxs/{tenant}/{site}{p['id']}", timeout=15) or {}
    info = d.get("jobPostingInfo") or {}
    locs = [info.get("location", "")] + (info.get("additionalLocations") or [])
    return {
        "structured": info.get("remoteType", "") or p.get("remoteType", ""),
        "location": ", ".join(l for l in locs if l) or p["location"],
        "desc": strip_html(info.get("jobDescription", "")),
        "url": info.get("externalUrl") or p["url"],
    }


def oracle_cloud_search(host, site_number, public_base, title):
    q = urllib.parse.quote(search_terms_for(title))
    url = (
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        f"?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site_number},limit=20,keyword={q}"
    )
    d = fetch(url, timeout=15)
    items = (d or {}).get("items") or []
    reqs = items[0].get("requisitionList", []) if items else []
    return [
        {
            "ats": "oracle_cloud",
            "host": host,
            "site_number": site_number,
            "id": j.get("Id"),
            "title": j.get("Title", ""),
            "location": j.get("PrimaryLocation", ""),
            "url": f"{public_base.rstrip('/')}/{j.get('Id')}",
            "wpt": j.get("WorkplaceType", ""),
        }
        for j in reqs
        if j.get("Id")
    ]


def oracle_cloud_detail(p):
    url = (
        f"https://{p['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        f"?onlyData=true&finder=ById;Id=%22{p['id']}%22,siteNumber={p['site_number']}"
    )
    d = fetch(url, timeout=15)
    items = (d or {}).get("items") or []
    info = items[0] if items else {}
    return {
        "structured": info.get("WorkplaceType", "") or p.get("wpt", ""),
        "location": info.get("PrimaryLocation", "") or p.get("location", ""),
        "desc": strip_html(info.get("ExternalDescriptionStr", "")),
    }


def ibm_search(title):
    q = search_terms_for(title)
    d = fetch(
        "https://www-api.ibm.com/search/api/v2",
        {
            "appId": "careers",
            "scopes": ["careers2"],
            "_source": ["title", "url", "description", "field_keyword_05", "field_keyword_17"],
            "query": {"bool": {"must": [{"multi_match": {"query": q, "fields": ["title", "description"]}}]}},
            "size": 20,
        },
        timeout=15,
    )
    hits = ((d or {}).get("hits") or {}).get("hits") or []
    out = []
    for h in hits:
        src = h.get("_source") or {}
        if not src.get("url"):
            continue
        out.append(
            {
                "ats": "ibm_careers",
                "title": src.get("title", ""),
                "location": src.get("field_keyword_05", ""),
                "url": src.get("url", ""),
                "wpt": src.get("field_keyword_17", ""),
                "desc": strip_html(src.get("description", "")),
            }
        )
    return out


def atlassian_board():
    d = fetch("https://www.atlassian.com/endpoint/careers/listings", timeout=20)
    if not isinstance(d, list):
        return []
    out = []
    for j in d:
        jid = j.get("id")
        if not jid:
            continue
        locs = ", ".join(j.get("locations") or [])
        desc = strip_html(" ".join(str(j.get(k, "") or "") for k in ("overview", "responsibilities", "qualifications")))
        out.append(
            {
                "ats": "atlassian",
                "id": jid,
                "title": j.get("title", ""),
                "location": locs,
                "url": f"https://www.atlassian.com/company/careers/details/{jid}",
                "wpt": "Remote" if "remote" in locs.lower() else "",
                "desc": desc,
            }
        )
    return out


def amazon_search(title):
    q = urllib.parse.quote(search_terms_for(title))
    d = fetch(f"https://www.amazon.jobs/en/search.json?base_query={q}&result_limit=20", timeout=15)
    jobs = (d or {}).get("jobs") or []
    return [
        {
            "ats": "amazon_jobs",
            "title": j.get("title", ""),
            "location": j.get("normalized_location") or j.get("location") or "",
            "url": f"https://www.amazon.jobs{j['job_path']}",
            "desc": strip_html((j.get("description") or "") + " " + (j.get("basic_qualifications") or "")),
        }
        for j in jobs
        if j.get("job_path")
    ]


def jibe_search(base_url, title):
    q = search_terms_for(title)
    d = cached(
        ("jibe", base_url, q),
        lambda: fetch(f"{base_url.rstrip('/')}/api/jobs?keywords={urllib.parse.quote(q)}&limit=20", timeout=15),
    )
    jobs = (d or {}).get("jobs") or []
    out = []
    for item in jobs:
        g = item.get("data") or {}
        slug = g.get("slug") or g.get("req_id")
        if not slug:
            continue
        loc_parts = [g.get("location_name") or "", g.get("full_location") or ""]
        loc = ", ".join(dict.fromkeys(p for p in loc_parts if p))
        out.append(
            {
                "ats": "jibe",
                "title": g.get("title", ""),
                "location": loc,
                "url": f"{base_url.rstrip('/')}/careers-home/jobs/{slug}",
                "wpt": "Remote" if "remote" in loc.lower() else "",
                "desc": strip_html(g.get("description", "")),
            }
        )
    return out


def avature_search(portal_base, title):
    q = search_terms_for(title)
    url = f"{portal_base.rstrip('/')}/SearchJobs/{urllib.parse.quote(q)}"
    final_url, html = cached(("avature", url), lambda: fetch_html_page(url, timeout=12))
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(final_url, a["href"])
        if "/JobDetail/" not in href or href in seen:
            continue
        txt = a.get_text(" ", strip=True)
        if not txt or "share " in txt.lower():
            continue
        seen.add(href)
        out.append({"ats": "avature", "title": txt, "location": "", "url": href})
    return out


def talentbrew_search(portal_base, title):
    q = search_terms_for(title)
    url = f"{portal_base.rstrip('/')}/search-jobs?k={urllib.parse.quote(q)}"
    final_url, html = cached(("talentbrew", url), lambda: fetch_html_page(url, timeout=12))
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(final_url, a["href"])
        if "/job/" not in href.lower() or href in seen:
            continue
        h2 = a.find(["h2", "h3", "span"])
        txt = (h2.get_text(" ", strip=True) if h2 else "") or a.get_text(" ", strip=True)
        loc_el = a.find(class_=re.compile(r"location", re.I))
        loc = loc_el.get_text(" ", strip=True) if loc_el else ""
        if not txt or len(txt) < 4:
            continue
        seen.add(href)
        out.append({"ats": "talentbrew", "title": txt, "location": loc, "url": href})
    return out


def eightfold_search(base_url, domain, title):
    q = urllib.parse.quote(search_terms_for(title))
    url = f"{base_url.rstrip('/')}/api/apply/v2/jobs?domain={urllib.parse.quote(domain)}&query={q}&num=20"
    d = fetch(url, timeout=12)
    positions = (d or {}).get("positions") or []
    return [
        {
            "ats": "eightfold",
            "title": p.get("name", ""),
            "location": p.get("location", ""),
            "url": p.get("canonicalPositionUrl") or f"{base_url.rstrip('/')}/careers/job/{p.get('id')}",
            "wpt": p.get("work_location_option", ""),
            "desc": strip_html(p.get("job_description", "")),
        }
        for p in positions
        if p.get("name") and (p.get("canonicalPositionUrl") or p.get("id"))
    ]


KNOWN_ENTERPRISE_SEARCH = {
    "oracle": lambda t: oracle_cloud_search("eeho.fa.us2.oraclecloud.com", "CX_45001", "https://careers.oracle.com/en/sites/jobsearch/job", t),
    "ibm": ibm_search,
    "amazon": amazon_search,
    "github": lambda t: jibe_search("https://www.github.careers", t),
    "jack henry": lambda t: avature_search("https://jackhenry.avature.net/careers", t),
    "deloitte": lambda t: avature_search("https://apply.deloitte.com/en_US/careers", t),
    "capital one": lambda t: talentbrew_search("https://www.capitalonecareers.com", t),
    "optum": lambda t: talentbrew_search("https://careers.unitedhealthgroup.com", t),
    "netflix": lambda t: eightfold_search("https://explore.jobs.netflix.net", "netflix.com", t),
}


# ---------------- Tier 3: Automatic Company Website Discovery ----------------
def lookup_company_domains(company):
    """Find official domains via Clearbit Suggest + clean slug fallbacks."""
    domains = []
    q = urllib.parse.quote(company.strip())
    suggestions = fetch(f"https://autocomplete.clearbit.com/v1/companies/suggest?query={q}", timeout=6, retries=0)
    if isinstance(suggestions, list):
        for item in suggestions[:2]:
            dom = (item or {}).get("domain", "").strip().lower()
            if dom and "." in dom and dom not in domains:
                domains.append(dom)
    for s in slugs_for(company)[:2]:
        for tld in ("com", "ai", "io"):
            dom = f"{s}.{tld}"
            if dom not in domains:
                domains.append(dom)
    return domains[:5]


def extract_career_signals(page_url, html):
    """Extract embedded ATS boards, Workday tenants, and direct job links from a career page."""
    signals = {
        "ats_slugs": [],
        "workday": [],
        "avature": [],
        "oracle": [],
        "eightfold": [],
        "talentbrew": [],
        "jibe": [],
        "direct_jobs": [],
    }
    blob = f"{page_url}\n{html}"

    for pat, ats_name in [
        (r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", "greenhouse"),
        (r"[?&]for=([a-z0-9_-]+).*?greenhouse", "greenhouse"),
        (r"https?://jobs\.ashbyhq\.com/([a-z0-9_-]+)", "ashby"),
        (r"https?://jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]+)", "lever"),
        (r"https?://jobs\.smartrecruiters\.com/([a-z0-9_-]+)", "smartrecruiters"),
        (r"https?://apply\.workable\.com/([a-z0-9_-]+)", "workable"),
        (r"https?://ats\.rippling\.com/([a-z0-9_-]+)", "rippling"),
        (r"https?://jobs\.gem\.com/([a-z0-9_-]+)", "gem"),
        (r"https?://app\.dover\.com/(?:jobs|apply)/([a-z0-9_-]+)", "dover"),
        (r"https?://([a-z0-9_-]+)\.pinpointhq\.com", "pinpoint"),
        (r"https?://([a-z0-9_-]+)\.recruitee\.com", "recruitee"),
        (r"https?://([a-z0-9_-]+)\.breezy\.hr", "breezy"),
    ]:
        for slug in re.findall(pat, blob, re.I):
            if slug.lower() not in {"embed", "api", "v1", "jobs", "apply"}:
                signals["ats_slugs"].append((ats_name, slug))

    for tenant, wd, site in re.findall(
        r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
        blob,
        re.I,
    ):
        if site.lower() not in {"wday", "cxs"}:
            signals["workday"].append((f"{tenant}.{wd}.myworkdayjobs.com", tenant, site))

    for av_url in re.findall(r"https?://[a-z0-9.-]+\.avature\.net/[a-z0-9_-]+", blob, re.I):
        signals["avature"].append(av_url)

    for ef_host in re.findall(r"https?://([a-z0-9.-]+\.eightfold\.ai)", blob, re.I):
        if ef_host.lower() != "app.eightfold.ai":
            signals["eightfold"].append(f"https://{ef_host}")

    if "/search-jobs" in blob.lower():
        parsed = urllib.parse.urlparse(page_url)
        if parsed.scheme and parsed.netloc:
            signals["talentbrew"].append(f"{parsed.scheme}://{parsed.netloc}")

    if "/api/jobs" in html:
        parsed = urllib.parse.urlparse(page_url)
        if parsed.scheme and parsed.netloc:
            signals["jibe"].append(f"{parsed.scheme}://{parsed.netloc}")

    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            items = data if isinstance(data, list) else [data]
            for d in items:
                if isinstance(d, dict) and d.get("@type") == "JobPosting" and d.get("title"):
                    signals["direct_jobs"].append(
                        {
                            "ats": "company_website",
                            "title": d.get("title", ""),
                            "location": str(d.get("jobLocation") or ""),
                            "url": d.get("url") or page_url,
                            "wpt": "Remote" if d.get("jobLocationType") == "TELECOMMUTE" else "",
                            "desc": strip_html(d.get("description", "")),
                        }
                    )
        except Exception:
            pass

    seen_urls = set()
    ats_hosts = (
        "greenhouse.io",
        "ashbyhq.com",
        "lever.co",
        "smartrecruiters.com",
        "workable.com",
        "rippling.com",
        "gem.com",
        "dover.com",
        "pinpointhq.com",
        "recruitee.com",
        "breezy.hr",
        "myworkdayjobs.com",
    )
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(page_url, a["href"])
        if href in seen_urls or not is_external_url(href):
            continue
        parsed_href = urllib.parse.urlparse(href)
        if any(h in parsed_href.netloc.lower() for h in ats_hosts):
            continue
        if any(seg in href.lower() for seg in ("/job/", "/jobs/", "/position/", "/positions/", "/opening/", "/openings/", "/careers/", "gh_jid=")):
            txt = a.get_text(" ", strip=True)
            if txt and 6 <= len(txt) <= 140:
                seen_urls.add(href)
                signals["direct_jobs"].append(
                    {"ats": "company_website", "title": txt, "location": "", "url": href}
                )

    return signals


def discover_company_career_info(company):
    """Discover ATS links or direct job postings from a company's own website."""
    domains = lookup_company_domains(company)
    candidate_urls = []
    for dom in domains[:3]:
        candidate_urls.extend(
            [
                f"https://www.{dom}/careers",
                f"https://careers.{dom}",
                f"https://jobs.{dom}",
                f"https://{dom}/careers",
            ]
        )

    merged = {
        "ats_slugs": [],
        "workday": [],
        "avature": [],
        "oracle": [],
        "eightfold": [],
        "talentbrew": [],
        "jibe": [],
        "direct_jobs": [],
        "domains": domains,
    }
    fetched_any = False
    for url in candidate_urls[:6]:
        final_u, html = fetch_html_page(url, timeout=6)
        if not html or len(html) < 500:
            continue
        fetched_any = True
        sig = extract_career_signals(final_u, html)
        for k in ("ats_slugs", "workday", "avature", "oracle", "eightfold", "talentbrew", "jibe", "direct_jobs"):
            merged[k].extend(sig[k])
        if sig["ats_slugs"] or sig["workday"] or sig["direct_jobs"]:
            break
    merged["fetched_any"] = fetched_any
    return merged


# ---------------- Remote & Cross-Filter Verification ----------------
def desc_flags(desc, patterns):
    hits = []
    for pat in patterns:
        m = re.search(pat, desc or "", re.I)
        if m:
            s = max(0, m.start() - 50)
            hits.append(desc[s : m.end() + 50].strip())
    return hits


def html_detail(url):
    """Fetch an external HTML job page and extract JSON-LD JobPosting, metadata, and description."""
    _, html = fetch_html_page(url, timeout=10)
    if not html:
        return {"structured": "", "location": "", "extra": "", "desc": "", "page_title": ""}

    soup = BeautifulSoup(html, "html.parser")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    structured, loc, desc = "", "", ""

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            raw = json.loads(s.string or "")
            items = raw if isinstance(raw, list) else [raw]
            for d in items:
                if not isinstance(d, dict) or d.get("@type") != "JobPosting":
                    continue
                if d.get("jobLocationType") == "TELECOMMUTE":
                    structured = "Remote"
                jl = d.get("jobLocation")
                if jl:
                    loc = str(jl)
                if d.get("description"):
                    desc = strip_html(d["description"])
                if d.get("title"):
                    page_title = d["title"]
        except Exception:
            pass

    meta_parts = []
    for tag in soup.find_all(["span", "div", "p", "li"], class_=re.compile(r"workplace|location|work-type|job-detail|metadata", re.I)):
        txt = tag.get_text(" ", strip=True)
        if txt and len(txt) < 160:
            meta_parts.append(txt)

    if not desc:
        body = soup.find("main") or soup.find("article") or soup.body
        desc = strip_html(body.get_text(" ", strip=True) if body else "")[:8000]

    return {
        "structured": structured,
        "location": loc,
        "extra": " | ".join(meta_parts[:8]),
        "desc": desc,
        "page_title": page_title,
    }


def fetch_posting_details(p):
    """Normalize detail fields across all ATS APIs and external career web pages (memoized by URL)."""
    key = ("detail", p.get("ats", ""), p.get("url", ""))
    return cached(key, lambda: _fetch_posting_details_uncached(p))


def _fetch_posting_details_uncached(p):
    ats = p.get("ats", "")
    url = p["url"]
    loc = p.get("location", "")
    structured = p.get("wpt", "") or p.get("remoteType", "")
    extra = ""
    desc = p.get("desc", "")

    if ats == "greenhouse":
        d = gh_detail(p)
        loc, extra, desc = d["location"], d["extra"], d["desc"]
    elif ats == "smartrecruiters":
        desc = sr_detail(p)
    elif ats == "workday":
        d = workday_detail(p)
        structured, loc, desc, url = d["structured"], d["location"], d["desc"], d["url"]
    elif ats == "oracle_cloud":
        d = oracle_cloud_detail(p)
        structured, loc, desc = d["structured"], d["location"], d["desc"]
    elif ats in ("avature", "talentbrew", "company_website", "rippling", "workable", "dover", "breezy") and not desc:
        d = html_detail(url)
        structured = structured or d["structured"]
        loc = loc or d["location"]
        extra = d["extra"]
        desc = d["desc"]

    return url, loc, structured, extra, desc


TITLE_REMOTE_RE = re.compile(
    r"\bremote\b(?!\s+(?:sensing|control|monitoring|access|management|diagnostics))",
    re.I,
)


def classify_remote_us(ats, loc, structured, extra, desc, title=""):
    """Classify external posting into (status, evidence)."""
    s = (structured or "").lower().replace("-", "").replace(" ", "")
    meta_txt = f"{loc} {extra}".strip()

    if s == "hybrid":
        return "HYBRID", f"{ats} workplaceType={structured}"
    if s in ("onsite", "inoffice"):
        return "ONSITE", f"{ats} workplaceType={structured}"
    if title and re.search(r"\bhybrid\b", title, re.I):
        return "HYBRID", f"external title indicates hybrid: {title[:100]}"
    if title and re.search(r"\b(on-?site|in[- ]office)\b(?!\s+reliability)", title, re.I):
        return "ONSITE", f"external title indicates onsite: {title[:100]}"
    if re.search(r"\bhybrid\b", meta_txt, re.I):
        return "HYBRID", f"location/metadata: {meta_txt[:120]}"
    if re.search(r"\b(on-?site|in[- ]office)\b(?!\s+reliability)", meta_txt, re.I) and not re.search(r"\bremote\b", meta_txt, re.I):
        return "ONSITE", f"location/metadata: {meta_txt[:120]}"

    remote_struct = s == "remote"
    remote_loc = bool(re.search(r"\b(remote|anywhere|work from home|wfh|virtual|telecommute|distributed)\b", meta_txt, re.I))
    remote_title = bool(title and TITLE_REMOTE_RE.search(title))
    if not (remote_struct or remote_loc or remote_title):
        weak = desc_flags(desc, WEAK_DESC)
        if weak:
            return "HYBRID", f"no remote label; description: ...{weak[0][:140]}..."
        if loc and COUNTRY_ONLY.match(loc):
            return "UNCLEAR", f"location is country-level only ({loc}), no explicit Remote label"
        if loc:
            return "NOT_REMOTE", f"physical location only, no remote signal: {meta_txt[:140]}"
        return "UNCLEAR", f"no remote signal on external page (type: {structured or 'n/a'})"

    if loc and NON_US.search(loc) and not (US_HINT.search(loc) or US_STATE.search(loc)):
        return "NON_US", f"remote outside US: {loc[:120]}"

    if remote_struct:
        src = f"{ats} workplaceType=Remote"
    elif remote_loc:
        src = f"location/metadata: {meta_txt[:120]}"
    else:
        src = f"external title remote: {title[:100]}"
    strong = desc_flags(desc, STRONG_DESC)
    if strong:
        return "UNCLEAR", f"{src}, but description says: ...{strong[0][:140]}..."
    return "REMOTE", src


def verify_external_job(row, match):
    """Cross-verify all filters on the resolved external career URL and return enriched record."""
    url, loc, structured, extra, desc = fetch_posting_details(match)
    ext_title = match.get("title") or row["job_title"]
    company = row["company"]
    reasons = []

    if not is_external_url(url):
        reasons.append("not a valid non-LinkedIn HTTPS external URL")

    domain = urllib.parse.urlparse(url).netloc.lower()
    if any(agg in domain for agg in AGGREGATOR_DOMAINS):
        reasons.append(f"aggregator domain ({domain})")

    blocked, block_reason = is_blacklisted_company(company=company, title=ext_title, url=url)
    if blocked:
        reasons.append(block_reason)

    if DISALLOWED_TITLE_RE.search(row["job_title"]) or DISALLOWED_TITLE_RE.search(ext_title):
        reasons.append("title indicates Director / VP / Vice role")

    if JUNIOR_RE.search(ext_title) or JUNIOR_RE.search(row["job_title"]):
        reasons.append("title indicates junior / entry-level / associate role (< 5 years experience)")

    if not SENIOR_RE.search(ext_title):
        reasons.append("external title does not confirm Senior/Lead/Staff/Principal seniority (5+ years experience)")

    if not is_software_or_ai_domain(ext_title) and not is_software_or_ai_domain(row.get("role_domain", "")):
        reasons.append("not an AI or Software domain")

    remote_status, remote_evidence = classify_remote_us(match["ats"], loc, structured, extra, desc, title=ext_title)
    if remote_status != "REMOTE":
        reasons.append(f"external remote/US check failed ({remote_status}: {remote_evidence})")

    return {
        "filter_status": "PASS" if not reasons else "REJECT",
        "remote_status": remote_status,
        "job_title": row["job_title"],
        "company": company,
        "external_apply_url": url,
        "ats": match["ats"],
        "ats_title": ext_title,
        "ats_location": loc,
        "remote_evidence": remote_evidence,
        "rejection_reasons": "; ".join(reasons),
        "match_score": match["_score_str"],
        "posted_minutes_ago": row.get("posted_minutes_ago", ""),
        "role_domain": row.get("role_domain", ""),
    }


# ---------------- Matching & Resolution Orchestration ----------------
def best_match(title, postings):
    scored = sorted(((title_score(title, p["title"]), p) for p in postings), key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.90:
        return None, 0.0
    top = scored[0][0]
    tied = [p for sc, p in scored if sc >= top - 0.001]

    def pref(p):
        txt = f"{p.get('location','')} {p.get('wpt','')} {p.get('remoteType','')}"
        is_remote = bool(re.search(r"remote", txt, re.I))
        non_us = bool(NON_US.search(p.get("location", ""))) and not (
            US_HINT.search(p.get("location", "")) or US_STATE.search(p.get("location", ""))
        )
        return (non_us, p.get("ats") == "company_website", not is_remote)

    tied.sort(key=pref)
    return tied[0], top


def find_tier1_boards(company):
    boards = []
    for slug in slugs_for(company):
        for name, fn in BOARD_FNS:
            b = cached((name, slug), lambda fn=fn, slug=slug: fn(slug))
            if b:
                boards.append(b)
    return boards


def find_enterprise_candidates(company, title):
    key = company.strip().lower()
    if key in KNOWN_ENTERPRISE_SEARCH:
        if key == "atlassian":
            board = cached(("atlassian", "all"), atlassian_board)
            return board or []
        return KNOWN_ENTERPRISE_SEARCH[key](title)
    return []


def resolve_external_posting(row):
    """Find the best exact external job posting across Tier 1, Tier 2, and Tier 3."""
    title, company = row["job_title"], row["company"]

    # Tier 1: Direct public ATS APIs
    candidates = []
    for b in find_tier1_boards(company):
        candidates.extend(b)
    match, score = best_match(title, candidates)

    # Tier 2: Known Workday & enterprise career portals
    if not match:
        ent = find_enterprise_candidates(company, title)
        if ent:
            match, score = best_match(title, ent)
    if not match:
        wd = workday_search(company, title)
        if wd:
            match, score = best_match(title, wd)

    # Tier 3: Automatic company career website discovery
    if not match:
        disc = cached(("discover", company.lower()), lambda c=company: discover_company_career_info(c))
        disc_candidates = list(disc["direct_jobs"])

        all_fns = dict(BOARD_FNS)
        all_fns.update(EXTRA_BOARD_FNS)
        for ats_name, slug in disc["ats_slugs"]:
            fn = all_fns.get(ats_name)
            if fn:
                b = cached((ats_name, slug), lambda fn=fn, slug=slug: fn(slug))
                if b:
                    disc_candidates.extend(b)

        if disc["workday"]:
            disc_candidates.extend(workday_search(company, title, extra_cfgs=disc["workday"]))
        for av in disc["avature"][:2]:
            disc_candidates.extend(avature_search(av, title))
        for tb in disc["talentbrew"][:2]:
            disc_candidates.extend(talentbrew_search(tb, title))
        for jb in disc["jibe"][:2]:
            disc_candidates.extend(jibe_search(jb, title))
        for ef in disc["eightfold"][:2]:
            for dom in disc["domains"][:2]:
                disc_candidates.extend(eightfold_search(ef, dom, title))

        if disc_candidates:
            match, score = best_match(title, disc_candidates)

    if not match:
        return None

    match = dict(match)
    match["_score_str"] = f"{score:.2f}"
    return verify_external_job(row, match)


def deduplicate_results(results):
    """Keep PASS rows first and deduplicate by canonical external URL."""
    ordered = sorted(
        results,
        key=lambda r: (
            0 if r.get("filter_status") == "PASS" else 1,
            (r.get("company") or "").lower(),
            (r.get("job_title") or "").lower(),
        ),
    )
    seen_urls, deduped = set(), []
    for r in ordered:
        key = canonicalize_url(r.get("external_apply_url", ""))
        if not key or key in seen_urls:
            continue
        seen_urls.add(key)
        deduped.append(r)
    return deduped


def write_outputs(all_resolved, out_csv):
    passing = [r for r in all_resolved if r["filter_status"] == "PASS"]

    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_resolved)

    with open("remote_external_links_24h.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(passing)

    with open("external_links.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["external_apply_url"])
        for r in passing:
            w.writerow([r["external_apply_url"]])

    return passing


def main():
    with open(IN_CSV, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(
        f"Resolving {len(rows)} jobs across {len({r['company'] for r in rows})} companies "
        "(external company/ATS URLs only, zero LinkedIn URLs, cross-verifying all filters)..."
    )
    done = [0]

    def work(r):
        res = resolve_external_posting(r)
        with _lock:
            done[0] += 1
            if done[0] % 50 == 0:
                print(f"  {done[0]}/{len(rows)}")
        return res

    with ThreadPoolExecutor(max_workers=20) as ex:
        raw_results = [r for r in ex.map(work, rows) if r is not None]

    all_resolved = deduplicate_results(raw_results)
    passing = write_outputs(all_resolved, OUT_CSV)

    print("\nFilter status counts:", dict(Counter(r["filter_status"] for r in all_resolved)))
    print("Remote status counts:", dict(Counter(r["remote_status"] for r in all_resolved)))
    print("Platform counts (PASS):", dict(Counter(r["ats"] for r in passing)))
    print(
        f"Wrote {len(all_resolved)} resolved external jobs to {OUT_CSV}, "
        f"and {len(passing)} verified passing jobs to remote_external_links_24h.csv & external_links.csv"
    )


if __name__ == "__main__":
    main()
