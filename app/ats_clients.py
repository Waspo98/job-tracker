import hashlib
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
import requests

from .http_client import get as http_get
from .url_safety import UnsafeUrlError, fetch_public_url

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

BOARD_URL_RE = re.compile(
    r'(?:https?:)?//[^\s"\'<>)]*(?:greenhouse\.io|lever\.co)[^\s"\'<>)]*',
    re.I
)


def _keywords_match(title, keywords_str):
    """Match any positive keyword while excluding titles with negative keywords."""
    terms = [k.strip().lower() for k in keywords_str.split(',') if k.strip()]
    if not terms:
        return True

    positive = []
    negative = []
    for term in terms:
        if term.startswith('-') or term.startswith('!'):
            excluded = term[1:].strip()
            if excluded:
                negative.append(excluded)
        else:
            positive.append(term)

    title_lower = title.lower()
    if any(term in title_lower for term in negative):
        return False
    if not positive:
        return True
    return any(term in title_lower for term in positive)


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


def _normalise_urlish_value(value):
    value = (value or '').strip()
    if not value:
        return ''
    value = value.replace('\\/', '/').strip(' "\'<>),;')
    if value.startswith('//'):
        return f'https:{value}'
    if value.startswith('http://') or value.startswith('https://'):
        return value
    if 'greenhouse.io' in value or 'lever.co' in value:
        return f'https://{value.lstrip("/")}'
    return value


def _clean_slug(value):
    value = (value or '').strip().strip('/')
    if re.fullmatch(r'[A-Za-z0-9_-]+', value):
        return value
    return ''


def _greenhouse_slug_from_url(value):
    parsed = urlparse(_normalise_urlish_value(value))
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split('/') if part]

    if host == 'boards-api.greenhouse.io' and len(parts) >= 3 and parts[:2] == ['v1', 'boards']:
        return _clean_slug(parts[2])

    if host not in ('boards.greenhouse.io', 'job-boards.greenhouse.io'):
        return ''

    if parts[:2] == ['embed', 'job_board']:
        return _clean_slug(parse_qs(parsed.query).get('for', [''])[0])

    if parts and parts[0] != 'embed':
        return _clean_slug(parts[0])

    return _clean_slug(parse_qs(parsed.query).get('for', [''])[0])


def _lever_slug_from_url(value):
    parsed = urlparse(_normalise_urlish_value(value))
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split('/') if part]

    if host == 'api.lever.co' and len(parts) >= 3 and parts[:2] == ['v0', 'postings']:
        return _clean_slug(parts[2])
    if host == 'jobs.lever.co' and parts:
        return _clean_slug(parts[0])
    return ''


def _candidate_board_values(html, soup):
    values = []
    for tag in soup.find_all(True):
        for attr in ('href', 'src', 'data-src', 'data-url', 'data-board-url'):
            value = tag.get(attr)
            if value:
                values.append(value)
    values.extend(match.group(0) for match in BOARD_URL_RE.finditer(html))
    return values


def _detect_supported_board(html, soup):
    for value in _candidate_board_values(html, soup):
        slug = _greenhouse_slug_from_url(value)
        if slug:
            return 'greenhouse', slug

        slug = _lever_slug_from_url(value)
        if slug:
            return 'lever', slug

    return None, None


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
    ats_type, slug = _detect_supported_board(resp.text, soup)
    if ats_type == 'greenhouse':
        return check_greenhouse(slug, keywords)
    if ats_type == 'lever':
        return check_lever(slug, keywords)

    # Remove script/style/nav/footer noise
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()

    candidates = []

    # Pass 1: links that look like job postings
    for a in soup.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href']
        if not href.startswith('http'):
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
