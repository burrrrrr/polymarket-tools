"""HTTP plumbing every read of Polymarket shares: the rate-limit retry, requests
sent in chunks, and the one connection the order path keeps open."""

import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from polymarket.parse import as_float

RATE_LIMIT_RETRIES = 3
RATE_LIMIT_WAIT_MAX_S = 5.0
CHUNK_THREADS = 8


def get_with_retry(http, url, params, timeout):
    """GET, waiting out a 429 up to RATE_LIMIT_RETRIES times; raises on a failed read."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        resp = http.get(url, params=params, timeout=timeout)
        if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            break
        time.sleep(min(as_float(resp.headers.get("retry-after")) or 1.0, RATE_LIMIT_WAIT_MAX_S))
    resp.raise_for_status()
    return resp


def chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def in_chunks(fetch, items, size):
    """[fetch(chunk)] for each chunk of the distinct items, in order.

    Chunks are fetched up to CHUNK_THREADS at a time, and a single chunk on the
    calling thread. What any chunk raises, this raises.
    """
    parts = chunks(list(dict.fromkeys(items)), size)
    if len(parts) <= 1:
        return [fetch(part) for part in parts]
    with ThreadPoolExecutor(min(len(parts), CHUNK_THREADS)) as pool:
        return list(pool.map(fetch, parts))


def persistent_client():
    """An httpx.Client over HTTP/2 whose idle connections never expire.

    httpx closes a connection that sits idle for 5 s, and the next request then
    pays a TCP and a TLS handshake, about 300 ms to the CLOB.
    """
    return httpx.Client(
        http2=True, limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=None))


def use_for_sdk(http):
    """Send every py_clob_client_v2 request through http, for the whole process:
    the SDK keeps one module-level client. Only a keyed entry point calls this."""
    # Imported here so a keyless process can import this module without loading the SDK.
    import py_clob_client_v2.http_helpers.helpers as sdk_http
    sdk_http._http_client = http
