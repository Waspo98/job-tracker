import ipaddress
import socket
from urllib.parse import urljoin, urlparse

from .http_client import build_session


class UnsafeUrlError(Exception):
    pass


def _is_public_ip(value):
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _host_is_safe(hostname, port):
    if not hostname:
        return False

    host = hostname.strip('[]').lower()
    if host in ('localhost', 'localhost.localdomain') or host.endswith('.localhost'):
        return False
    if host.endswith('.local') or '.' not in host:
        return False
    if _is_public_ip(host):
        return True

    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass

    try:
        resolved = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    ips = {item[4][0] for item in resolved}
    return bool(ips) and all(_is_public_ip(ip) for ip in ips)


def validate_public_http_url(url):
    url = (url or '').strip()
    parsed = urlparse(url)

    if parsed.scheme not in ('http', 'https'):
        return None, 'Please provide a URL starting with http:// or https://.'
    if not parsed.hostname:
        return None, 'Please provide a valid careers page URL.'
    if parsed.username or parsed.password:
        return None, 'URLs with embedded usernames or passwords are not supported.'

    try:
        port = parsed.port
    except ValueError:
        return None, 'Please provide a valid URL port.'

    if not _host_is_safe(parsed.hostname, port):
        return None, 'For safety, only public careers page URLs can be checked.'

    return url, None


def fetch_public_url(url, headers=None, timeout=15, max_redirects=5):
    current_url = url
    session = build_session()

    for _ in range(max_redirects + 1):
        safe_url, error = validate_public_http_url(current_url)
        if error:
            raise UnsafeUrlError(error)

        response = session.get(
            safe_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('Location')
            if not location:
                return response
            current_url = urljoin(response.url, location)
            continue

        return response

    raise UnsafeUrlError('Too many redirects while checking that careers page.')
