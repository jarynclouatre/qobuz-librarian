"""Last.fm client used by Discover."""
import threading
import time

import requests

from qobuz_librarian import config, redaction
from qobuz_librarian.api.client import ua_string
from qobuz_librarian.ui_cli.logging import vlog


class LastfmError(Exception):
    """Last.fm answered, but not with a result."""
    def __init__(self, message, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class LastfmKeyRejected(LastfmError):
    """The API key is invalid (10) or suspended (26)."""


class LastfmRateLimited(LastfmError):
    """Too many requests (29). Back off and resume later."""


class LastfmUnavailable(LastfmError):
    """Last.fm could not be reached, or could not answer (8, 11, 16, 5xx)."""


# Documented Last.fm error codes this module reacts to by name. Everything else
# becomes a plain LastfmError carrying the code.
_NOT_FOUND_CODE     = 6
_KEY_REJECTED_CODES = (10, 26)
_TEMPORARY_CODES    = (8, 11, 16)
_RATE_LIMIT_CODE    = 29

_REQUEST_TIMEOUT = max(2, int(config.WEB_FETCH_TIMEOUT) - 2)
_RETRY_STATUSES  = (500, 502, 503, 504)
_MAX_ATTEMPTS    = 3

# Minimum gap between two outbound requests, process-wide.
_MIN_INTERVAL = 0.25

_thread_local = threading.local()
_rate_lock = threading.Lock()
_last_request_at = 0.0


def _get_session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": ua_string()})
        _thread_local.session = s
    return s


def _sleep(seconds: float):
    """Indirection so tests can neutralise waiting without touching every other
    time.sleep in the codebase."""
    time.sleep(seconds)


def _throttle():
    """Wait until _MIN_INTERVAL has passed since the last request started.

    The lock is held across the wait, so two threads cannot both decide the gap
    has elapsed and fire together.
    """
    global _last_request_at
    with _rate_lock:
        wait = _last_request_at + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            _sleep(wait)
        _last_request_at = time.monotonic()


def _reset_for_tests():
    """Forget the rate-limiter's last-request time so an ordered test suite does
    not inherit a wait from the test before it."""
    global _last_request_at
    with _rate_lock:
        _last_request_at = 0.0


def api_key() -> str:
    """The configured key, registered for redaction so it is masked wherever it
    is written down. Read at call time: the Settings page rewrites the config
    global while the app runs."""
    key = str(getattr(config, "LASTFM_API_KEY", "") or "").strip()
    if key:
        redaction.register_secret(key)
    return key


def is_configured() -> bool:
    return bool(api_key())


def _net_reason(exc) -> str:
    """Short, human reason for a requests failure. Deliberately not the
    exception text: that carries the full URL, key included."""
    if isinstance(exc, requests.Timeout):
        return "Last.fm timed out"
    if isinstance(exc, requests.ConnectionError):
        return "couldn't reach Last.fm (network down or blocked?)"
    if isinstance(exc, requests.TooManyRedirects):
        return "too many redirects from Last.fm"
    return "a network error reaching Last.fm"


def _raise_for_code(code: int, message: str, method: str):
    text = message or "no reason given"
    if code in _KEY_REJECTED_CODES:
        raise LastfmKeyRejected(
            f"Last.fm rejected the API key ({text})", code=code)
    if code == _RATE_LIMIT_CODE:
        raise LastfmRateLimited(
            f"Last.fm is rate-limiting this key ({text})", code=code)
    if code in _TEMPORARY_CODES:
        raise LastfmUnavailable(
            f"Last.fm couldn't answer {method} ({text}) - try again later",
            code=code)
    raise LastfmError(f"Last.fm error {code} from {method}: {text}", code=code)


def lastfm_get(method: str, params: dict | None = None, *,
               api_key_override: str | None = None) -> dict:
    """One Last.fm call. Returns the decoded body, or {} when Last.fm has never
    heard of what was asked for (code 6)."""
    key = (api_key() if api_key_override is None
           else str(api_key_override or "").strip())
    if not key:
        raise LastfmKeyRejected("No Last.fm API key is set")
    redaction.register_secret(key)
    query = {
        **(params or {}),
        "method": method,
        "api_key": key,
        "format": "json",
        "autocorrect": 1,
    }
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        _throttle()
        try:
            r = _get_session().get(config.LASTFM_API_BASE, params=query,
                                   timeout=_REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt >= _MAX_ATTEMPTS:
                raise LastfmUnavailable(
                    f"{_net_reason(e)} (while calling {method})") from e
            wait = min(2 ** (attempt - 1), 8)
            vlog(f"{method}: network error; retry {attempt}/{_MAX_ATTEMPTS} in {wait}s")
            _sleep(wait)
            continue

        # The body decides, whatever the status: Last.fm serves a rejected key
        # as HTTP 403 with the real code inside, and an ordinary result as 200.
        body = None
        try:
            body = r.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("error") is not None:
            try:
                code = int(body.get("error"))
            except (TypeError, ValueError):
                raise LastfmError(
                    f"Last.fm sent an unreadable error code from {method}"
                ) from None
            if code == _NOT_FOUND_CODE:
                return {}
            _raise_for_code(code, str(body.get("message") or "").strip(), method)

        if r.status_code == 429:
            raise LastfmRateLimited(
                f"Last.fm is rate-limiting this key (HTTP 429 from {method})")
        if r.status_code in _RETRY_STATUSES:
            if attempt >= _MAX_ATTEMPTS:
                raise LastfmUnavailable(
                    f"Last.fm kept returning HTTP {r.status_code} after "
                    f"{attempt} attempt(s) (while calling {method}) - "
                    f"try again later")
            wait = min(2 ** (attempt - 1), 8)
            vlog(f"{method}: HTTP {r.status_code}; retry "
                 f"{attempt}/{_MAX_ATTEMPTS} in {wait}s")
            _sleep(wait)
            continue
        if r.status_code != 200:
            raise LastfmError(
                f"HTTP {r.status_code} from {method}", code=None)
        if not isinstance(body, dict):
            raise LastfmError(f"{method} returned a non-dict response")
        return body


def _rows(payload: dict, container: str, key: str) -> list:
    """Last.fm nests every list one level down, and collapses a single-entry
    list into a bare object. Both shapes come back as a list of dicts here."""
    block = payload.get(container)
    if not isinstance(block, dict):
        return []
    rows = block.get(key)
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, list):
        return [r for r in rows if isinstance(r, dict)]
    return []


def _name(row: dict, key: str = "name") -> str:
    return str(row.get(key) or "").strip()


def _artist_name(row: dict) -> str:
    """The artist on an album row, which arrives as an object or a bare name
    depending on the endpoint."""
    artist = row.get("artist")
    if isinstance(artist, dict):
        return str(artist.get("name") or "").strip()
    return str(artist or "").strip()


def _match(row: dict) -> float:
    """Similarity, 0-1. Last.fm sends it as a string, and omits it entirely on
    some rows; an unscored neighbour ranks last rather than disappearing."""
    try:
        value = float(row.get("match"))
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return min(max(value, 0.0), 1.0)


def _count(row: dict) -> int:
    try:
        return max(int(row.get("count")), 0)
    except (TypeError, ValueError):
        return 0


def _albums(payload: dict, container: str) -> list[dict]:
    out = []
    for row in _rows(payload, container, "album"):
        title = _name(row)
        artist = _artist_name(row)
        if title and artist:
            out.append({"artist": artist, "title": title})
    return out


def get_similar_artists(name: str, *, limit: int = 100) -> list[dict]:
    """Artists Last.fm considers similar to `name`, each with its 0-1 match."""
    if not str(name or "").strip():
        return []
    body = lastfm_get("artist.getSimilar", {"artist": name, "limit": limit})
    out = []
    for row in _rows(body, "similarartists", "artist"):
        artist = _name(row)
        if artist:
            out.append({"name": artist, "match": _match(row)})
    return out


def get_artist_top_tags(name: str) -> list[dict]:
    """Tags applied to `name`, most-used first, each with its 0-100 count."""
    if not str(name or "").strip():
        return []
    body = lastfm_get("artist.getTopTags", {"artist": name})
    out = []
    for row in _rows(body, "toptags", "tag"):
        tag = _name(row)
        if tag:
            out.append({"name": tag, "count": _count(row)})
    return out


def get_tag_top_albums(tag: str, *, page: int = 1,
                       limit: int = 50) -> list[dict]:
    """Albums tagged `tag`, most-tagged first."""
    if not str(tag or "").strip():
        return []
    body = lastfm_get("tag.getTopAlbums",
                      {"tag": tag, "page": page, "limit": limit})
    return _albums(body, "albums")


def get_tag_top_artists(tag: str, *, page: int = 1,
                        limit: int = 50) -> list[dict]:
    """Artists tagged `tag`, most-tagged first."""
    if not str(tag or "").strip():
        return []
    body = lastfm_get("tag.getTopArtists",
                      {"tag": tag, "page": page, "limit": limit})
    out = []
    for row in _rows(body, "topartists", "artist"):
        artist = _name(row)
        if artist:
            out.append({"name": artist})
    return out


def get_artist_top_albums(name: str, *, page: int = 1,
                          limit: int = 50) -> list[dict]:
    """The artist's most-played albums, which is how Discover picks which of a
    suggested artist's albums to show first."""
    if not str(name or "").strip():
        return []
    body = lastfm_get("artist.getTopAlbums",
                      {"artist": name, "page": page, "limit": limit})
    return _albums(body, "topalbums")


def probe_key(api_key_override: str | None = None) -> bool:
    """One cheap call that separates a bad key from Last.fm being down, so the
    Settings page can say which it is. A supplied key is checked directly even
    when applying it is deferred until an active job ends. Returns True, or
    raises."""
    lastfm_get("chart.getTopArtists", {"limit": 1},
               api_key_override=api_key_override)
    return True
