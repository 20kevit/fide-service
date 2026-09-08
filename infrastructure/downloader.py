"""Robust official-archive downloader. No framework imports.

Streamed to disk (never response.content), bounded timeouts, retries with
backoff ONLY for transient problems; permanent statuses fail fast. All
failures raise classified FideError.
"""
import logging
import os
import time
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

from domain.errors import FideError, FideErrorCategory
from domain.models import RATING_TYPES

DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_READ_TIMEOUT = 30
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 2.0
CHUNK_SIZE = 64 * 1024

#: Permanent refusals — retrying is pointless.
_PERMANENT_STATUS = {400, 401, 403, 404, 405, 410, 414, 416, 501}

_MONTH_IDS = ("jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec")


def build_rating_url(base_url: str, rating_type: str, period: str) -> str:
    """Official monthly per-type archive URL (pure function).

    e.g. standard_sep26frl_xml.zip. Raises ValueError — never guess a URL.
    """
    if rating_type not in RATING_TYPES:
        raise ValueError("rating_type must be one of %s" % (RATING_TYPES,))
    try:
        year_s, mon_s = period.split("-")
        year, month = int(year_s), int(mon_s)
    except (ValueError, AttributeError):
        raise ValueError("period must be YYYY-MM, got %r" % (period,))
    if not 1 <= month <= 12 or year < 2000:
        raise ValueError("period must be YYYY-MM, got %r" % (period,))
    return "%s%s_%s%02dfrl_xml.zip" % (
        base_url if base_url.endswith("/") else base_url + "/",
        rating_type, _MONTH_IDS[month - 1], year % 100)


def source_is_secure(url: str) -> bool:
    """True when a source URL uses HTTPS.

    Plain HTTP is tolerated only as an explicit operator override
    (``FIDE_DOWNLOAD_BASE_URL``) — never silently. Callers must warn.
    """
    try:
        return urlparse(url).scheme.lower() == "https"
    except (AttributeError, ValueError):
        return False


def verify_rating_url(url: str, timeout: int = 10) -> dict:
    """HEAD reachability check. Never downloads the body."""
    try:
        # verify=True is the requests default; stated explicitly so TLS
        # can never be disabled by accident in a future edit.
        resp = requests.head(url, timeout=timeout, allow_redirects=True,
                             verify=True)
        ok = resp.status_code == 200
        length = resp.headers.get("Content-Length", "")
        return {
            "ok": ok,
            "status": resp.status_code,
            "detail": ("OK (%s bytes)" % length)
            if ok and length.isdigit()
            else ("HTTP status %d" % resp.status_code),
        }
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "status": None,
                "detail": "Unreachable: %s" % exc}


def _classify_requests_exception(exc, url):
    host = urlparse(url).hostname or url
    text = str(exc)
    if isinstance(exc, requests.exceptions.SSLError):
        return FideError(FideErrorCategory.NETWORK_TLS_ERROR, stage="tls",
                         detail="TLS error connecting to %s: %s" % (host, text))
    if isinstance(exc, (requests.exceptions.ConnectTimeout,
                        requests.exceptions.ReadTimeout,
                        requests.exceptions.Timeout)):
        return FideError(FideErrorCategory.NETWORK_TIMEOUT, stage="connect",
                         detail="Timed out connecting to %s: %s" % (host, text))
    if isinstance(exc, requests.exceptions.HTTPError):
        # raise_for_status() failures (e.g. 5xx exhausted retries): the
        # host answered, so this is an HTTP problem, not a dead network.
        status = getattr(getattr(exc, "response", None),
                         "status_code", None)
        if status == 429:
            return FideError(FideErrorCategory.HTTP_RATE_LIMIT,
                             stage="download",
                             detail="FIDE host rate-limited the download "
                                    "(HTTP 429).")
        return FideError(FideErrorCategory.HTTP_ERROR, stage="download",
                         detail="FIDE host returned an HTTP error (HTTP %s)."
                         % status)
    if isinstance(exc, requests.exceptions.ConnectionError):
        lowered = text.lower()
        if any(k in lowered for k in ("getaddrinfo", "nodename",
                                      "name or service", "dns",
                                      "temporary failure in name")):
            return FideError(FideErrorCategory.NETWORK_DNS_ERROR, stage="dns",
                             detail="DNS resolution failed for %s: %s"
                             % (host, text))
        return FideError(FideErrorCategory.NETWORK_CONNECTION_ERROR,
                         stage="connect",
                         detail="Could not connect to %s: %s" % (host, text))
    return FideError(FideErrorCategory.NETWORK_CONNECTION_ERROR,
                     stage="connect",
                     detail="Network error for %s: %s" % (host, text))


def download_fide_zip(url, dest_zip_path,
                      progress_cb: Optional[Callable[[Optional[float]], None]] = None,
                      connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
                      read_timeout: int = DEFAULT_READ_TIMEOUT,
                      max_retries: int = DEFAULT_MAX_RETRIES,
                      backoff_base: float = DEFAULT_BACKOFF_BASE) -> int:
    """Stream a ZIP to ``dest_zip_path`` atomically (.part + replace).

    Returns the final size in bytes. Retries transient failures only.
    """
    os.makedirs(os.path.dirname(dest_zip_path) or ".", exist_ok=True)
    if not source_is_secure(url):
        # Audible, not silent: HTTP admits MITM content swaps. Allowed
        # only as a deliberate operator override; HTTPS is the default.
        log.warning("Downloading FIDE archive over plain HTTP: %s",
                    urlparse(url).hostname or "unknown-host")
    tmp_path = dest_zip_path + ".part"
    attempt = 0
    while True:
        try:
            with requests.get(url, stream=True,
                              timeout=(connect_timeout, read_timeout),
                              verify=True) as resp:
                if resp.status_code == 429:
                    raise FideError(
                        FideErrorCategory.HTTP_RATE_LIMIT, stage="download",
                        detail="FIDE host rate-limited the download (HTTP 429).")
                if resp.status_code in _PERMANENT_STATUS:
                    raise FideError(
                        FideErrorCategory.HTTP_ERROR, stage="download",
                        detail="FIDE host refused the download (HTTP %d)."
                        % resp.status_code)
                resp.raise_for_status()
                total = resp.headers.get("Content-Length")
                total = int(total) if total and total.isdigit() else None
                downloaded = 0
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb and total:
                            progress_cb(downloaded / total)
                head = open(tmp_path, "rb").read(4)
                if head != b"PK\x03\x04":
                    raise FideError(
                        FideErrorCategory.INVALID_CONTENT, stage="download",
                        detail="Downloaded content is not a ZIP archive "
                               "(possible error page).")
                os.replace(tmp_path, dest_zip_path)
                return os.path.getsize(dest_zip_path)
        except FideError:
            _drop_partial(tmp_path)
            raise
        except requests.exceptions.RequestException as exc:
            _drop_partial(tmp_path)
            attempt += 1
            transient = True
            status = getattr(getattr(exc, "response", None),
                             "status_code", None)
            if status in _PERMANENT_STATUS:
                transient = False
            if not transient or attempt > max_retries:
                raise _classify_requests_exception(exc, url)
            time.sleep(backoff_base ** attempt)


def _drop_partial(tmp_path: str) -> None:
    try:
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass
