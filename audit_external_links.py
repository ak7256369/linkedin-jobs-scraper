import csv
import json
import re
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

AGGREGATOR_DOMAINS = {
    'jobera.com', 'torre.ai', 'grad.jobs', 'freehire.me', 'relomote.com',
    'pk.trabajo.org', 'nortjobs.com', 'remoteyeah.com', 'himalayas.app',
    'realjobsfromanywhere.com', 'bestremotejobs.com', 'www.bestremotejobs.com',
    'underdog.io', 'recruitslab.com', 'jobs.hiringourheroes.org', 'haystackapp.io'
}

BLACKLISTED_COMPANIES = {
    'clickup', 'jobright.ai', 'jobright', 'alignerr', 'ladders', 'rex.zone',
    'jobtogether', 'dayforce', 'micro1', 'data annotation', 'dataannotation',
    'turing', 'chatgpt jobs', 'synthesia'
}

DISALLOWED_TITLES = re.compile(r'\b(director|vp|vice\s*president|vice)\b', re.IGNORECASE)

def fetch_html(url, timeout=12):
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='ignore')

def check_ashby(url, html):
    # Ashby embeds JSON in script tags and has workplaceType
    # e.g. "workplaceType":"OnSite" or "workplaceType":"Remote" or "workplaceType":"Hybrid"
    m_wpt = re.search(r'"workplaceType"\s*:\s*"([^"]+)"', html)
    wpt = m_wpt.group(1).strip() if m_wpt else None
    
    # Also check JSON-LD
    telecommute = False
    soup = BeautifulSoup(html, 'html.parser')
    title = ''
    for s in soup.find_all('script', type='application/ld+json'):
        try:
            d = json.loads(s.string)
            if isinstance(d, dict):
                if d.get('jobLocationType') == 'TELECOMMUTE':
                    telecommute = True
                if d.get('title'):
                    title = d.get('title')
        except:
            pass
            
    if wpt == 'OnSite' or wpt == 'Hybrid':
        return False, f"Ashby workplaceType is '{wpt}'"
    if wpt == 'Remote' or telecommute:
        return True, "Ashby Remote verified"
        
    # Check visible page text for "Location Type" -> "On-site"
    if re.search(r'Location Type\s*On-site', html, re.IGNORECASE) or re.search(r'Location Type\s*Hybrid', html, re.IGNORECASE):
        return False, "Ashby page indicates On-site/Hybrid"
        
    return False, f"Ashby unknown workplaceType ({wpt})"

def check_greenhouse(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    
    # JSON-LD check
    telecommute = False
    json_loc = ""
    for s in soup.find_all('script', type='application/ld+json'):
        try:
            d = json.loads(s.string)
            if isinstance(d, dict):
                if d.get('jobLocationType') == 'TELECOMMUTE':
                    telecommute = True
                loc = d.get('jobLocation')
                if loc:
                    json_loc = str(loc)
        except:
            pass
            
    # Greenhouse location element
    loc_el = soup.find(class_=re.compile(r'location|body--metadata', re.I))
    loc_text = loc_el.get_text(strip=True) if loc_el else ""
    
    # Check workplace tag or metadata
    m_wp = re.search(r'workplace\s*type\s*:\s*([^<\n,]+)', html, re.I)
    wp_val = m_wp.group(1).strip() if m_wp else ""
    
    if re.search(r'\b(on-site|onsite|in-office)\b', wp_val, re.I):
        return False, f"Greenhouse workplace is {wp_val}"
    if re.search(r'\bhybrid\b', wp_val, re.I):
        return False, f"Greenhouse workplace is Hybrid"
        
    if telecommute or re.search(r'\bremote\b', loc_text, re.I) or re.search(r'\bremote\b', json_loc, re.I) or re.search(r'\bremote\b', wp_val, re.I):
        return True, f"Greenhouse Remote verified ({loc_text or wp_val})"
        
    # If location is physical without remote
    if loc_text and not re.search(r'\bremote\b', loc_text, re.I):
        return False, f"Greenhouse location is physical only: '{loc_text}'"
        
    return False, "Greenhouse no remote tag found"

def check_lever(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    
    # Check workplaceTypes tag: e.g. <span class="workplaceTypes">Hybrid</span>
    wp_el = soup.find(class_=re.compile(r'workplace', re.I))
    wp_text = wp_el.get_text(strip=True) if wp_el else ""
    
    loc_el = soup.find(class_=re.compile(r'location', re.I))
    loc_text = loc_el.get_text(strip=True) if loc_el else ""
    
    if re.search(r'\b(hybrid|on-site|onsite|in-office)\b', wp_text, re.I):
        return False, f"Lever workplace is '{wp_text}'"
        
    if re.search(r'\bremote\b', wp_text, re.I) or re.search(r'\bremote\b', loc_text, re.I):
        return True, f"Lever Remote verified ({wp_text or loc_text})"
        
    # JSON-LD check
    for s in soup.find_all('script', type='application/ld+json'):
        try:
            d = json.loads(s.string)
            if isinstance(d, dict) and d.get('jobLocationType') == 'TELECOMMUTE':
                return True, "Lever JSON-LD TELECOMMUTE"
        except:
            pass
            
    return False, f"Lever not verified remote (workplace: '{wp_text}', loc: '{loc_text}')"

def check_workday(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    # Workday pages typically have JSON-LD or meta tags
    telecommute = False
    for s in soup.find_all('script', type='application/ld+json'):
        try:
            d = json.loads(s.string)
            if isinstance(d, dict):
                if d.get('jobLocationType') == 'TELECOMMUTE':
                    telecommute = True
                loc = d.get('jobLocation')
                if loc and re.search(r'\bremote\b', str(loc), re.I):
                    telecommute = True
        except:
            pass
            
    if telecommute:
        return True, "Workday JSON-LD TELECOMMUTE"
        
    # Check if URL itself has location like /job/US-*-* or Remote
    if 'remote' in url.lower():
        return True, "Workday URL indicates Remote"
        
    # Check meta tags
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    desc = meta_desc.get('content', '') if meta_desc else ''
    if re.search(r'\b(hybrid|on-site|onsite)\b', desc[:300], re.I):
        return False, f"Workday description starts with hybrid/onsite: {desc[:60]}"
    if re.search(r'\b(remote worker|fully remote|work from home|telecommute|location:\s*remote)\b', desc, re.I):
        return True, "Workday description mentions remote worker"
        
    # If the page text has "Time Type: Full time" and "Locations: Remote"
    m_loc = re.search(r'Locations?[:\s]+([^\n<]+)', html, re.I)
    if m_loc and re.search(r'\bremote\b', m_loc.group(1), re.I):
        return True, f"Workday Locations: {m_loc.group(1)}"
        
    return False, "Workday not confirmed remote"

def check_generic(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    
    # 1. JSON-LD check
    for s in soup.find_all('script', type='application/ld+json'):
        try:
            d = json.loads(s.string)
            if isinstance(d, dict):
                if d.get('jobLocationType') == 'TELECOMMUTE':
                    return True, "JSON-LD TELECOMMUTE"
                loc = str(d.get('jobLocation', ''))
                if re.search(r'\bremote\b', loc, re.I):
                    return True, f"JSON-LD jobLocation has remote: {loc}"
        except:
            pass

    # 2. Look for explicit On-site / Hybrid tags in the header / metadata
    # Often in tags with class/id matching location, workplace, meta
    meta_tags = soup.find_all(['span', 'div', 'p', 'li'], class_=re.compile(r'workplace|location|work-type|job-detail|metadata', re.I))
    for t in meta_tags:
        txt = t.get_text(' ', strip=True)
        if re.search(r'\b(on-site|onsite|in-office)\b', txt, re.I) and not re.search(r'\bremote\b', txt, re.I):
            return False, f"Explicit metadata on-site: '{txt[:50]}'"
        if re.search(r'\bhybrid\b', txt, re.I) and not re.search(r'\bremote\b', txt, re.I):
            return False, f"Explicit metadata hybrid: '{txt[:50]}'"
            
    # Check if 'remote' is in location or title
    title_el = soup.find('title')
    title_text = title_el.get_text(strip=True) if title_el else ""
    if re.search(r'\bremote\b', title_text, re.I):
        return True, f"Title contains Remote: {title_text}"
        
    # Check body for structured "Location: Remote" or "City: Remote" (e.g. Maximus)
    if re.search(r'\b(City|Location|Workplace|Workplace Type|Work Location)\s*:\s*Remote\b', html, re.I):
        return True, "Structured Location: Remote found"
        
    return False, "Generic ATS could not confirm Remote"

def audit_url(url):
    domain = urllib.parse.urlparse(url).netloc.lower()
    
    # 1. Check aggregator
    if any(agg in domain for agg in AGGREGATOR_DOMAINS):
        return False, f"Purged aggregator domain: {domain}"
        
    # 2. Check blacklisted company
    for bl in BLACKLISTED_COMPANIES:
        if bl in domain or bl in url.lower():
            return False, f"Blacklisted company in URL: {bl}"
            
    # 3. Check Director / VP
    if DISALLOWED_TITLES.search(url):
        return False, f"Disallowed title in URL: {url}"
        
    try:
        html = fetch_html(url)
    except Exception as e:
        return False, f"Fetch error: {e}"
        
    # Check page title for blacklisted companies or Director/VP
    soup_quick = BeautifulSoup(html[:4000], 'html.parser')
    title_tag = soup_quick.find('title')
    title_str = title_tag.get_text(strip=True) if title_tag else ""
    if DISALLOWED_TITLES.search(title_str):
        return False, f"Disallowed title in page title: '{title_str}'"
    for bl in BLACKLISTED_COMPANIES:
        if bl in title_str.lower():
            return False, f"Blacklisted company in page title: '{title_str}'"
            
    # Dispatch by platform
    if 'ashbyhq.com' in domain:
        return check_ashby(url, html)
    elif 'greenhouse.io' in domain:
        return check_greenhouse(url, html)
    elif 'lever.co' in domain:
        return check_lever(url, html)
    elif 'myworkdayjobs.com' in domain:
        return check_workday(url, html)
    else:
        return check_generic(url, html)

if __name__ == '__main__':
    with open('external_links.csv', 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        all_urls = [row[0].strip() for row in reader if row and row[0].strip() and not row[0].startswith('external_apply_url')]
        
    print(f"Auditing {len(all_urls)} URLs with ThreadPoolExecutor...")
    
    accepted = []
    rejected = []
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(audit_url, u): u for u in all_urls}
        for future in as_completed(future_to_url):
            u = future_to_url[future]
            try:
                ok, reason = future.result()
                if ok:
                    accepted.append((u, reason))
                else:
                    rejected.append((u, reason))
            except Exception as exc:
                rejected.append((u, f"Exception: {exc}"))
                
    print(f"\nAudit complete! Accepted: {len(accepted)}, Rejected: {len(rejected)}")
    print("\n--- SAMPLE REJECTED (first 25) ---")
    for u, reason in rejected[:25]:
        print(f"REJECTED: {u}\n  Reason: {reason}\n")
        
    print("\n--- SAMPLE ACCEPTED (first 10) ---")
    for u, reason in accepted[:10]:
        print(f"ACCEPTED: {u}\n  Reason: {reason}\n")
        
    # Check specifically Handshake
    handshake_found = [r for r in rejected if 'handshake' in r[0].lower()]
    print(f"Handshake in rejected: {handshake_found}")

