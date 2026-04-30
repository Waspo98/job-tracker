import hashlib
import re
import requests
from bs4 import BeautifulSoup
from http_client import get as http_get
from url_safety import UnsafeUrlError, fetch_public_url

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

# Patterns that suggest a string is a job title rather than nav/footer noise
JOB_SIGNALS = re.compile(
    r'\b(engineer|developer|designer|manager|analyst|scientist|director|coordinator|'
    r'specialist|consultant|architect|lead|head of|vp |vice president|recruiter|'
    r'intern|associate|senior|junior|staff|principal|product|research|marketing|'
    r'sales|operations|finance|legal|hr |human resources|data|software|hardware|'
    r'mechanical|electrical|clinical|regulatory|quality|manufacturing)\b',
    re.I
)

# Things to ignore even if they contain job-signal words
NOISE_PATTERNS = re.compile(
    r'^(home|about|contact|privacy|terms|blog|news|press|login|sign|careers$|jobs$|'
    r'search|filter|sort|apply|back|next|prev|load more|show more)$',
    re.I
)


def _keywords_match(title, keywords_str):
    """Return True if ANY keyword appears in the job title (case-insensitive). Empty = match all."""
    if not keywords_str.strip():
        return True
    keywords = [k.strip().lower() for k in keywords_str.split(',') if k.strip()]
    title_lower = title.lower()
    return any(kw in title_lower for kw in keywords)


def _make_job_id(text, url=''):
    """Stable hash of job title + url for deduplication."""
    return hashlib.sha1(f"{text.strip().lower()}|{url}".encode()).hexdigest()[:16]


def _extract_location(element, soup=None):
    """
    Try to find a location near a job title element.
    Looks for common location patterns in sibling/child text.
    """
    LOC_SIGNALS = re.compile(
        r'\b(remote|hybrid|onsite|on-site|chicago|new york|san francisco|austin|'
        r'boston|seattle|denver|atlanta|miami|london|berlin|toronto|nationwide|'
        r'united states|usa|u\.s\.|anywhere|\w+,\s*[A-Z]{2})\b',
        re.I
    )

    candidates = []

    for child in element.find_all(True):
        text = child.get_text(strip=True)
        if text and len(text) < 60 and LOC_SIGNALS.search(text):
            candidates.append(text)

    for sib in list(element.next_siblings)[:3]:
        if hasattr(sib, 'get_text'):
            text = sib.get_text(strip=True)
            if text and len(text) < 60 and LOC_SIGNALS.search(text):
                candidates.append(text)

    if element.parent:
        for child in element.parent.find_all(True):
            text = child.get_text(strip=True)
            if text and text != element.get_text(strip=True) and len(text) < 60 and LOC_SIGNALS.search(text):
                candidates.append(text)

    return candidates[0] if candidates else ''


def _looks_like_job(text):
    """Heuristic: does this text look like a job title?"""
    text = text.strip()
    if not text or len(text) < 5 or len(text) > 150:
        return False
    if NOISE_PATTERNS.match(text):
        return False
    if JOB_SIGNALS.search(text):
        return True
    return False



def check_custom_url(url, keywords):
    """
    Scrape a careers page and return job-like entries matching keywords.
    Uses a multi-pass strategy:
      1. Find <li> and <a> elements that look like job titles
      2. Deduplicate by text content
      3. Filter by keywords
    """
    try:
        resp = fetch_public_url(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except UnsafeUrlError as e:
        return [], f"Could not fetch {url}: {e}"
    except requests.exceptions.HTTPError as e:
        return [], f"Could not fetch {url}: HTTP {e.response.status_code}"
    except Exception as e:
        return [], f"Could not fetch {url}: {e}"

    soup = BeautifulSoup(resp.text, 'html.parser')

    # Remove script/style/nav/footer noise
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()

    candidates = []

    # Pass 1: links that look like job postings
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']
        if not href.startswith('http'):
            from urllib.parse import urljoin
            href = urljoin(url, href)
        if _looks_like_job(text):
            candidates.append({'text': text, 'href': href, 'el': a})

    # Pass 2: list items
    if len(candidates) < 3:
        for li in soup.find_all('li'):
            text = li.get_text(strip=True)
            a = li.find('a', href=True)
            href = ''
            if a:
                href = a['href']
                if not href.startswith('http'):
                    from urllib.parse import urljoin
                    href = urljoin(url, href)
            if _looks_like_job(text):
                candidates.append({'text': text, 'href': href, 'el': li})

    # Pass 3: headings
    if len(candidates) < 3:
        for tag in soup.find_all(['h2', 'h3', 'h4']):
            text = tag.get_text(strip=True)
            if _looks_like_job(text):
                candidates.append({'text': text, 'href': '', 'el': tag})

    # Deduplicate by normalised text and apply keyword filter
    seen_texts = set()
    matches = []
    for c in candidates:
        norm = c['text'].lower().strip()
        if norm in seen_texts:
            continue
        seen_texts.add(norm)
        if _keywords_match(c['text'], keywords):
            location = _extract_location(c['el']) if c.get('el') else ''
            matches.append({
                'job_id': _make_job_id(c['text'], c['href']),
                'title': c['text'],
                'location': location,
                'url': c['href'],
            })

    return matches, None


def check_greenhouse(slug, keywords):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = http_get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        matches = []
        for job in resp.json().get('jobs', []):
            title = job.get('title', '')
            if _keywords_match(title, keywords):
                matches.append({
                    'job_id': str(job['id']),
                    'title': title,
                    'location': job.get('location', {}).get('name', 'Unknown'),
                    'url': job.get('absolute_url', ''),
                })
        return matches, None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return [], f"Greenhouse slug '{slug}' not found."
        return [], f"Greenhouse API error: {e}"
    except Exception as e:
        return [], f"Error reaching Greenhouse: {e}"


def check_lever(slug, keywords):
    url = f"https://api.lever.co/v0/postings/{slug}"
    try:
        resp = http_get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        matches = []
        for job in resp.json():
            title = job.get('text', '')
            if _keywords_match(title, keywords):
                categories = job.get('categories', {})
                location = categories.get('location') or \
                           (categories.get('allLocations') or ['Unknown'])[0]
                matches.append({
                    'job_id': job['id'],
                    'title': title,
                    'location': location,
                    'url': job.get('hostedUrl', ''),
                })
        return matches, None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return [], f"Lever slug '{slug}' not found."
        return [], f"Lever API error: {e}"
    except Exception as e:
        return [], f"Error reaching Lever: {e}"


def check_watch(watch):
    if watch['ats_type'] == 'greenhouse':
        return check_greenhouse(watch['ats_slug'], watch['keywords'])
    elif watch['ats_type'] == 'lever':
        return check_lever(watch['ats_slug'], watch['keywords'])
    elif watch['ats_type'] == 'custom':
        return check_custom_url(watch['careers_url'], watch['keywords'])
    else:
        return [], "Unsupported ATS type; cannot check automatically."
