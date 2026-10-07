"""Shared HTTP session: retries, timeouts, and per-platform politeness.

Politeness is a requirement here, not a nicety. Several employer boards sit behind
one platform -- a dozen Workday tenants are all Workday -- so pacing per hostname
still hammers the platform. Limits are keyed by platform instead, and every
request through this session waits its turn across all threads.
"""
import os
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# (connect, read) -- a server that trickles bytes hits the read timeout per chunk.
TIMEOUT = (10, 25)

# Transient connect failures happen; retry rather than losing the employer.
RETRY = Retry(
    total=3,
    connect=3,
    read=2,
    status=2,
    status_forcelist=(429, 500, 502, 503, 504),
    backoff_factor=1.5,
    respect_retry_after_header=True,
    allowed_methods=frozenset(["GET", "POST"]),
    raise_on_status=False,
)

# Minimum seconds between requests to the same platform, across every thread.
# Workday is the strict one: many tenants, one back end, and it refuses
# connections outright rather than returning 429.
PLATFORM_LIMITS = {
    "myworkdayjobs.com": 1.2,
    "greenhouse.io": 0.4,
    "ashbyhq.com": 0.4,
    "lever.co": 0.5,
    "smartrecruiters.com": 0.5,
    "amazon.jobs": 0.6,
    "dice.com": 0.5,
    "linkedin.com": 2.5,
}
DEFAULT_INTERVAL = 0.3

# Several instances on one host share a public IP, so they must share the pace.
# POLITENESS_SCALE lets a second instance slow itself down: 2.0 halves its rate.
SCALE = float(os.environ.get("POLITENESS_SCALE", "1.0"))

# Cooperative cancellation. ThreadPoolExecutor.shutdown(cancel_futures=True)
# only drops queued work -- a worker already inside a fetch keeps issuing
# requests, so phase-one threads were still hitting employer boards while
# phase two ran. Every request checks this before waiting its turn.
STOP = threading.Event()


class Cancelled(RuntimeError):
    """Raised when a worker is asked to stop between requests."""


_lock = threading.Lock()
_next_free: dict[str, float] = {}


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for suffix in PLATFORM_LIMITS:
        if host.endswith(suffix):
            return suffix
    return host


def wait_turn(url: str) -> None:
    """Block until this platform's next slot is free."""
    if STOP.is_set():
        raise Cancelled("run phase ended; abandoning in-flight source")
    key = platform_of(url)
    interval = PLATFORM_LIMITS.get(key, DEFAULT_INTERVAL) * SCALE
    while True:
        with _lock:
            now = time.monotonic()
            ready = _next_free.get(key, 0.0)
            if now >= ready:
                _next_free[key] = now + interval
                return
            sleep_for = ready - now
        time.sleep(sleep_for)


# Some platforms are simply slow to answer, which is not the same as refusing.
# Workday takes over 20 seconds to complete a connect from this container -- a
# single hand-run request returns 200 in 20.1s -- so a 10-second connect timeout
# recorded every Workday tenant as a failure while the service was working. Wait
# longer for those; do not send more.
PLATFORM_TIMEOUTS = {
    "myworkdayjobs.com": (30, 60),
}


class PoliteAdapter(HTTPAdapter):
    """Paces every request by platform before it goes out."""

    def send(self, request, **kwargs):
        wait_turn(request.url)
        override = PLATFORM_TIMEOUTS.get(platform_of(request.url))
        if override is not None:
            connect, read = override
            kwargs["timeout"] = (connect * SCALE, read * SCALE)
        return super().send(request, **kwargs)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    adapter = PoliteAdapter(max_retries=RETRY, pool_connections=16, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s
