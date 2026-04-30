import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_TIMEOUT = 12
RETRY_STATUSES = (502, 503, 504)


def build_session():
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.4,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset(['GET']),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def get(url, **kwargs):
    kwargs.setdefault('timeout', DEFAULT_TIMEOUT)
    with build_session() as session:
        return session.get(url, **kwargs)
