"""Runtime configuration.

Every value here is overridable via an environment variable of the same
name; the literal in this file is just the fallback when the env is
unset. `compose.yaml` sets the ones a container deployment needs.
"""
import math
import os
import sys
from pathlib import Path


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def _env(key, default):
    """Return env var cast to the same type as default, or default if unset."""
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        parsed = type(default)(val)
    except (ValueError, TypeError):
        _warn(f"{key}={val!r} is not a valid {type(default).__name__}; "
              f"using default {default!r}")
        return default
    if isinstance(parsed, float) and not math.isfinite(parsed):
        _warn(f"{key}={val!r} must be finite; using default {default!r}")
        return default
    return parsed


_TRUE_TOKENS  = {"1", "true", "yes", "on", "y", "t"}
_FALSE_TOKENS = {"0", "false", "no", "off", "n", "f"}


def _env_bool(key, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    v = val.strip().lower()
    if not v:                       # `${VAR:-}` in compose resolves to empty
        return default
    if v in _TRUE_TOKENS:
        return True
    if v in _FALSE_TOKENS:
        return False
    _warn(f"{key}={val!r} is not a valid boolean; using default {default!r}")
    return default


def _env_choice(key, default: str, choices) -> str:
    """Lowercased env value restricted to `choices`, else `default`.

    An explicit value outside the set warns instead of silently degrading.
    A typo'd LYRICS_FORMAT shouldn't quietly stop sidecars being written.
    """
    val = os.environ.get(key)
    if val is None:
        return default
    v = val.strip().lower()
    if v in choices:
        return v
    if v:
        _warn(f"{key}={val!r} must be one of {', '.join(choices)}; "
              f"using default {default!r}")
    return default


def _env_log_level() -> str:
    level = _env_choice(
        "LOG_LEVEL",
        "info",
        ("debug", "info", "warning", "error", "critical", "warn", "fatal"),
    )
    return {"warn": "WARNING", "fatal": "CRITICAL"}.get(level, level.upper())


def _env_downsample_choice():
    """The keep-originals choice from env (keep/delete), or None when unset.

    Tri-state, not a bool: None means unchosen, so the first downsample asks.
    keep/delete is a conscious choice. It shows selected in Settings and never
    prompts."""
    v = (os.environ.get("DOWNSAMPLE_KEEP_ORIGINALS") or "").strip().lower()
    if v in ("keep", "delete"):
        return v
    if v:
        _warn(f"DOWNSAMPLE_KEEP_ORIGINALS must be keep or delete; got {v!r}. "
              "leaving it unchosen so the first downsample asks")
    return None


def _env_path(key, default: Path) -> Path:
    val = os.environ.get(key)
    return Path(val) if val else default


def _env_num_min(key, default, minimum, maximum=None):
    """A numeric env value floored at `minimum`, optionally ceilinged at
    `maximum`, warning when it has to clamp.

    Two bad-override failure modes share the floor guard. A thread-pool size
    of 0 or negative crashes the pool constructor at startup. A negative
    delay/cooldown handed to time.sleep raises ValueError and takes down the
    worker thread, and a negative subprocess timeout makes every download
    time out instantly. Clamp loudly at the boundary so no consumer has to
    defend against it.

    The optional `maximum` guards against the dual case: `ARTIST_SCAN_WORKERS
    =999999` would otherwise spawn thousands of threads at scan start. Clamp +
    warn there too so a typo'd value doesn't take the box down.
    """
    val = _env(key, default)
    if val < minimum:
        _warn(f"{key}={val!r} is below the minimum of {minimum}; using {minimum}.")
        return minimum
    if maximum is not None and val > maximum:
        _warn(f"{key}={val!r} is above the maximum of {maximum}; using {maximum}.")
        return maximum
    return val


def _env_unit_float(key, default):
    """A similarity threshold env value clamped to the valid 0.0–1.0 range.

    Every fuzzy-match threshold is a 0..1 score cutoff, and several gate
    destructive review paths (consolidate, artist/dir matching). A value that
    falls outside the range can only come from a typo'd `.env`, and it changes
    behaviour far from intent: CONSOLIDATE_THRESH=-1 (or any negative) would make
    every sibling folder a consolidation candidate, while a value above 1 makes
    nothing ever match and silently disables the feature. Clamp into range and
    warn rather than trust the bad value. (0.0 and 1.0 are the legitimate
    boundaries, "match everything" and "exact only", so they pass through.)"""
    val = _env(key, default)
    if val < 0.0:
        _warn(f"{key}={val!r} is below 0.0 (thresholds are 0–1 scores); using 0.0.")
        return 0.0
    if val > 1.0:
        _warn(f"{key}={val!r} is above 1.0 (thresholds are 0–1 scores); using 1.0.")
        return 1.0
    return val


def _resolve_secret(key: str) -> str:
    """Value of `key`, falling back to the file named by `{key}_FILE`.

    The file form lets Docker/Compose secrets supply the token
    without it showing up in `docker inspect` or a process listing.

    The resolved value is deliberately NOT written back to os.environ: doing so
    re-exported the secret into every subprocess the app spawns (rip, beet,
    flac, and the operator's POST_JOB_HOOK shell), defeating the whole point of
    the *_FILE form. Callers must read the config global (e.g.
    cfg.QOBUZ_USER_AUTH_TOKEN), never os.environ[key]."""
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = os.environ.get(f"{key}_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        _warn(f"{key}_FILE={path!r} couldn't be read ({e}); ignoring it.")
        return ""


# Auth: direct env-var override (no streamrip config file needed)
# Set these in compose.yaml or the Settings page writes them to the config file.
# QOBUZ_USER_AUTH_TOKEN_FILE points at a file holding the token instead (Docker
# secrets), keeping it out of the container's environment.
QOBUZ_USER_AUTH_TOKEN = _resolve_secret("QOBUZ_USER_AUTH_TOKEN")
QOBUZ_USER_ID         = os.environ.get("QOBUZ_USER_ID", "")

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME             = Path.home()

# streamrip config lives at /config/streamrip/config.toml inside the container,
# falls back to the host location for dev/CLI usage outside Docker.
STREAMRIP_CONFIG = _env_path(
    "STREAMRIP_CONFIG",
    HOME / ".config" / "streamrip" / "config.toml",
)

MUSIC_ROOT  = _env_path(
    "MUSIC_ROOT",
    Path("/music") if os.environ.get("QL_IN_CONTAINER") else HOME / "Music",
)

def _sibling_of_music(name: str) -> Path:
    """A sibling directory of MUSIC_ROOT. If MUSIC_ROOT is at the
    filesystem root (e.g. /music → /), put the sibling under ~/.cache
    instead of polluting /. Used for STAGING_DIR / UPGRADE_BACKUP_DIR
    defaults when running outside Docker without explicit env vars."""
    parent = MUSIC_ROOT.parent
    if parent == Path("/"):
        return HOME / ".cache" / "qobuz-librarian" / name
    return parent / name

STAGING_DIR = _env_path("STAGING_DIR", _sibling_of_music(".staging"))

# Library migration (the one-time "organise an existing collection" tool).
# Source = the library to read; destination = where the organised copy
# is built. Read from the environment so both can be mounted into the
# container; the CLI's --migrate-src / --migrate-dest override them. Empty
# means "unset"; the tool then prompts (or errors with how to set them).
MIGRATE_SRC  = os.environ.get("MIGRATE_SRC", "").strip()
MIGRATE_DEST = os.environ.get("MIGRATE_DEST", "").strip()

# Beets is bundled inside the container (compose.yaml sets these to
# /config/beets). The fallback is beets' own standard config location, so
# a bare-metal / `pip install` run finds an existing beets setup.
BEETS_CONFIG_DIR = _env_path(
    "BEETS_CONFIG_DIR",
    HOME / ".config" / "beets",
)
BEETS_DB_PATH = _env_path(
    "BEETS_DB_PATH",
    BEETS_CONFIG_DIR / "musiclibrary.db",
)
# Normally inferred from the `beet` console script. This explicit path covers
# uncommon launchers whose shebang does not expose their Python environment.
BEETS_PYTHON = os.environ.get("BEETS_PYTHON", "").strip()

# Optional beets path/naming overrides. Empty = leave beets to its own
# config.yaml (fully editable in the /config volume) / built-in defaults.
# Set these to control folder/file structure without hand-editing YAML,
# e.g. BEETS_PATH_DEFAULT="$albumartist/$album ($year)/$track - $title".
BEETS_PATH_DEFAULT   = os.environ.get("BEETS_PATH_DEFAULT", "").strip()
BEETS_PATH_SINGLETON = os.environ.get("BEETS_PATH_SINGLETON", "").strip()
BEETS_PATH_COMP      = os.environ.get("BEETS_PATH_COMP", "").strip()

# Comma-separated list of beets plugins to choose. Empty honours whatever is
# set in /config/beets/config.yaml. A custom list replaces those choices for
# managed imports; the import override adds inline, artwork requirements, and
# the internal guard plugins. For example:
# BEETS_PLUGINS="fetchart,lastgenre,replaygain,scrub"
BEETS_PLUGINS = [p.strip() for p in
                 os.environ.get("BEETS_PLUGINS", "").split(",")
                 if p.strip()]

# Where fetched cover art ends up:
#   sidecar = a cover image file in the album folder (beets `fetchart`, default)
#   embed   = embedded in the track tags only, no leftover file (`embedart`)
#   both    = a cover file AND embedded art
ARTWORK = _env_choice("ARTWORK", "sidecar", ("sidecar", "embed", "both"))

# COMPOSE_FILE is only used by the legacy docker-exec beets fallback (when
# `beet` isn't on PATH and a compose file exists. This is never the case in the
# bundled image, where beets is called directly). Default is a CWD-relative
# name so the fallback simply stays inactive unless a user opts in via env.
COMPOSE_FILE = _env_path(
    "COMPOSE_FILE",
    Path("compose.yaml"),
)

# State and data files all live under DATA_DIR so one volume covers them.
# Default to ~/.local/share/qobuz-librarian (XDG) outside Docker; the
# container sets DATA_DIR=/data so this default is only used by CLI dev
# installs (pipx / pip install -e).
_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or (HOME / ".local" / "share"))
DATA_DIR = _env_path(
    "DATA_DIR",
    _XDG_DATA / "qobuz-librarian",
)

FETCH_LOG_FILE       = DATA_DIR / ".qobuz_librarian_log.json"
LAST_SCAN_FILE       = DATA_DIR / ".qobuz_last_scan"
APP_LOG_FILE         = DATA_DIR / "qobuz-librarian.log"
LOG_LEVEL            = _env_log_level()
WALK_SEEN_FILE       = DATA_DIR / ".qobuz_walk_seen.txt"
ALBUM_WALK_SEEN_FILE = DATA_DIR / ".qobuz_album_walk_seen.txt"
PENDING_QUEUE_FILE   = DATA_DIR / ".qobuz_pending_queue.json"
QUEUE_JOURNAL_DIR    = DATA_DIR / ".qobuz_queue_journals"
LYRIC_RETRY_FILE     = DATA_DIR / ".qobuz_lyric_retry.json"
REPAIR_LOG_PATH      = DATA_DIR / ".qobuz_replaced_tracks.log"
CAPPED_FILE          = DATA_DIR / ".qobuz_upgrade_capped.json"
UPGRADE_STATE_FILE   = DATA_DIR / ".qobuz_upgrade_state.json"
DOWNSAMPLE_STATE_FILE = DATA_DIR / ".qobuz_downsample_state.json"
LIBRARY_SCAN_STATE_FILE = DATA_DIR / ".qobuz_library_scan_state.json"
REVIEW_BADGE_STATE_FILE = DATA_DIR / ".qobuz_review_badges.json"
# Albums the user dismissed from the bulk library/upgrade walks so they stop
# resurfacing on every scan. User-driven and durable (no auto-expiry, unlike
# CAPPED_FILE). See library/hidden.py.
HIDDEN_FILE          = DATA_DIR / ".qobuz_hidden.json"
# Fingerprints surfaced by the last completed library walk, per mode, so a
# re-run can badge albums that weren't there before ("new since last scan").
SCAN_SEEN_FILE       = DATA_DIR / ".qobuz_scan_seen.json"
# Per-artist snapshot of the Qobuz catalog (album ids) at the last new-release
# check, plus when it ran. The new-release quickscan diffs against this to
# surface only what's appeared since. See library/new_releases.py.
NEW_RELEASE_STATE_FILE = DATA_DIR / ".qobuz_new_releases.json"
# Progress for a resumable library scan (artists already done + albums found), so
# an interrupted full scan continues instead of restarting. Cleared on a clean
# finish or a deliberate cancel. See library/scan_checkpoint.py.
SCAN_CHECKPOINT_FILE = DATA_DIR / ".qobuz_scan_checkpoint.json"
# Artist name → resolved Qobuz artist id, cached so repeat scans skip the
# search round-trip. See library/discovery.py.
ARTIST_RESOLVE_CACHE_FILE = DATA_DIR / ".artist_resolve_cache.json"
# lyric_fetch's per-track state file. Its in-module default sits beside the
# module file (under /app in the image), which isn't writable after a PUID/PGID
# drop. The state couldn't be saved and "provider-unavailable" retries would
# silently never resume. DATA_DIR is the persistent, writable volume, route here.
LYRIC_FETCH_STATE_FILE = DATA_DIR / ".lyric_fetch_state.json"
# Web UI login: username + password hash + session secret, written 0600
# (it holds a credential). Lives in DATA_DIR with the rest of the state so
# one volume covers it. WEB_AUTH=none is the opt-out knob, checked live in
# web/auth.py.
WEB_AUTH_FILE = DATA_DIR / ".qobuz_web_auth.json"

# Lock file lives in DATA_DIR so the web container and a `docker compose
# run` CLI invocation share it via the /data volume (otherwise each
# container has its own /tmp and the lock can't see the other).
LOCK_FILE = _env_path(
    "LOCK_FILE",
    DATA_DIR / "qobuz_librarian.lock",
)
UPGRADE_BACKUP_DIR = _env_path(
    "UPGRADE_BACKUP_DIR",
    _sibling_of_music(".upgrade_backups"),
)


def validate_storage_roots() -> None:
    """Refuse storage trees that alias or contain one another.

    Library, staging, and retained-backup operations have different ownership
    rules. Letting any of their roots overlap makes exclusions ambiguous and
    can turn an in-progress download or backup into managed library content.
    Resolve existing symlinks before comparing so aliases fail closed too.
    """
    configured = (
        ("MUSIC_ROOT", MUSIC_ROOT),
        ("STAGING_DIR", STAGING_DIR),
        ("UPGRADE_BACKUP_DIR", UPGRADE_BACKUP_DIR),
    )
    resolved = []
    for name, value in configured:
        try:
            path = Path(os.path.abspath(os.fspath(value))).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError(f"{name} cannot be resolved safely: {exc}") from None
        resolved.append((name, path))

    for index, (name, path) in enumerate(resolved):
        for other_name, other in resolved[:index]:
            if path == other or path in other.parents or other in path.parents:
                raise ValueError(
                    f"{name} and {other_name} must be separate, non-nested "
                    "directories"
                )


# ── Web UI ────────────────────────────────────────────────────────────────────
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = _env("WEB_PORT", 8666)
# Compose always binds WEB_PORT=8666 inside the container, but may publish a
# different host port. CLI recovery links must name the public side.
WEB_PUBLIC_PORT = _env("WEB_PUBLIC_PORT", WEB_PORT)

# ── Versioned file schemas ────────────────────────────────────────────────────
PENDING_QUEUE_VERSION = 2
LYRIC_RETRY_VERSION   = 1

# ── API ───────────────────────────────────────────────────────────────────────
QOBUZ_API_BASE = os.environ.get("QOBUZ_API_BASE", "https://www.qobuz.com/api.json/0.2")
QOBUZ_APP_ID   = os.environ.get("QOBUZ_APP_ID",   "798273057")

# ── Download quality ──────────────────────────────────────────────────────────
# streamrip quality code: 2=CD/16-bit·44.1kHz lossless, 3=24-bit ≤96kHz,
# 4=24-bit ≤192kHz. Default 4: the highest quality your subscription serves
# (hi-res where Qobuz has it). Drop to 2 for CD lossless if you want smaller
# files. Passed to `rip -q`. The lossy MP3 tiers (0=128k, 1=320k) aren't
# supported: the library is FLAC-only and discards non-FLAC after the rip, so a
# lossy tier would fetch nothing, so both are coerced to tier 2 (CD lossless).
STREAMRIP_QUALITY = _env("STREAMRIP_QUALITY", 4)
if STREAMRIP_QUALITY in (0, 1):
    _warn(f"STREAMRIP_QUALITY={STREAMRIP_QUALITY} is a lossy MP3 tier "
          "(1=320kbps, 0=128kbps) the FLAC-only library can't keep. Using "
          "tier 2 (CD lossless) instead.")
    STREAMRIP_QUALITY = 2
elif STREAMRIP_QUALITY not in (2, 3, 4):
    # Out-of-range goes straight to `rip -q`, which then fails the download
    # with an opaque usage error. Fall back to the highest tier loudly.
    _warn(f"STREAMRIP_QUALITY={STREAMRIP_QUALITY!r} isn't a tier (2-4); using 4.")
    STREAMRIP_QUALITY = 4

# ── Lyrics ────────────────────────────────────────────────────────────────────
LYRICS_ENABLED   = _env_bool("LYRICS_ENABLED", True)
# How fetched lyrics are written: "embed" (FLAC tag), "sidecar" (.lrc file
# next to the track), or "both".
LYRICS_FORMAT    = _env_choice("LYRICS_FORMAT", "embed", ("embed", "sidecar", "both"))
# Comma-separated provider names to try, in order. Empty = built-in default.
LYRICS_PROVIDERS = [p.strip() for p in
                    os.environ.get("LYRICS_PROVIDERS", "").split(",")
                    if p.strip()]
# A provider library must not be able to park every lyrics worker forever.
# This is an application-level wall-clock budget around one provider lookup;
# timed-out providers count toward the existing per-run circuit breaker.
LYRICS_PROVIDER_TIMEOUT = _env_num_min("LYRICS_PROVIDER_TIMEOUT", 30.0, 1.0)

# ── Timeouts / delays ─────────────────────────────────────────────────────────
# Results shown on the user-facing Search page. 8 was too few for a major
# artist (a "Paul McCartney" search surfaced almost nothing); 25 shows a useful
# page. The CLI fetches more; a web "load more" beyond this is a future add.
SEARCH_LIMIT     = _env("SEARCH_LIMIT",     25)
# Per-context search depth for internal matchers. ARTIST_LOOKUP_LIMIT scores
# this many artist-search hits when resolving a name to an id; 5 was too few for
# common names (the right artist could rank 6th+ and silently resolve to
# nothing), so 15. CATALOG_SEARCH_LIMIT is the per-album edition-match depth.
# SEARCH_LIMIT above is the user-facing Search-page depth and is unaffected.
# Higher = more matches at the cost of more Qobuz API calls.
ARTIST_LOOKUP_LIMIT  = _env("ARTIST_LOOKUP_LIMIT",  15)
CATALOG_SEARCH_LIMIT = _env("CATALOG_SEARCH_LIMIT", 12)
# Per-album rip subprocess cap. 0 means no timeout (the rip runs until it
# finishes or is cancelled); a stray negative folds to 0 rather than killing
# every download the instant it starts.
RIP_TIMEOUT      = _env_num_min("RIP_TIMEOUT", 900, 0)
DELAY_BETWEEN    = _env_num_min("DELAY_BETWEEN", 1.0, 0.0)
# Free-space floor (MB) checked before each album download. streamrip only
# signals a full disk via stderr text (which detect_disk_full best-effort greps),
# so this preflight turns a near-full staging area into the proper disk-full stop
# instead of a silent run of per-track failures that delete each partial. 0
# disables the check.
MIN_FREE_STAGING_MB = _env_num_min("MIN_FREE_STAGING_MB", 500, 0)
# Pause before the next queued album when Qobuz throttling was detected
# in the last rip (vs the normal DELAY_BETWEEN). Tames the error-wave
# pattern on multi-hundred-album queues. Set 0 to disable.
RATE_LIMIT_COOLDOWN = _env_num_min("RATE_LIMIT_COOLDOWN", 30.0, 0.0)
# INACTIVITY timeout for the beets import (seconds of *zero output*),
# NOT a wall-clock cap: a slow-but-progressing import over R2 / a slow
# NAS keeps printing beets progress, so it is never killed no matter how
# long it runs. Only true silence this long means a genuinely hung
# import (DB lock, prompt, deadlocked plugin). Killing it stops the
# single web job worker from freezing forever. 0 disables the guard
# entirely (no timeout on any beets call), so a stuck import can hang the
# worker until restart; only set 0 if you'd rather never risk a false kill.
BEETS_TIMEOUT    = _env_num_min("BEETS_TIMEOUT", 600, 0)
# Per-album import: retry on idle-timeout up to N times with a short
# pause between, so a single transient stall doesn't strand the album.
# After exhausting retries the album's staged folder is moved aside
# (see BEETS_RETRY_DIR) so the rest of the queue keeps going.
BEETS_MAX_ATTEMPTS = _env_num_min("BEETS_MAX_ATTEMPTS", 2, 1)
BEETS_RETRY_PAUSE  = _env_num_min("BEETS_RETRY_PAUSE", 30, 0)
BEETS_RETRY_DIR    = os.environ.get("BEETS_RETRY_DIR", ".beets_retry")
# Per-album courtesy pause in the CLI walk. The 429 retry/backoff in
# api/client is the real throttle backstop, so 0 is fine in practice.
# Raise via env if Qobuz ever tightens its per-account rate limits.
ARTIST_API_DELAY = _env_num_min("ARTIST_API_DELAY", 0.0, 0.0)
# Concurrent artists during a library gap scan. Each worker has its own HTTP
# session, so this is real parallelism; kept modest so the request rate stays
# polite (the 429 retry/back-off in api/client is the backstop). 1 restores
# the old sequential behaviour.
ARTIST_SCAN_WORKERS = _env_num_min("ARTIST_SCAN_WORKERS", 4, 1, 16)

# Minimum gap (seconds) between live Qobuz ISRC lookups across all repair-scan
# workers. A deep repair sweep does one lookup per track, fanned out over
# ARTIST_SCAN_WORKERS, so a first scan of a big library could otherwise burst
# the account; this paces the live calls (cache hits skip it). The ISRC cache
# already makes re-scans and shared ISRCs free, so this only bounds the cold
# scan. 0 disables it; the 429 back-off in api/client is still the backstop.
REPAIR_LOOKUP_MIN_INTERVAL = _env_num_min("REPAIR_LOOKUP_MIN_INTERVAL", 0.05, 0.0)

# Cache get_album() responses on disk (DATA_DIR/album_cache.db). An album's track
# list is immutable, so this turns the per-owned-album fetch, the dominant cost
# of a library scan, into a local lookup on re-scans. Off disables it.
ALBUM_CACHE_ENABLED = _env_bool("ALBUM_CACHE_ENABLED", True)

# Cache parsed FLAC tags on disk (DATA_DIR/flac_cache.db), keyed on file
# mtime+size, so unchanged files aren't re-parsed by mutagen on every scan. The
# key self-invalidates when a file is edited/replaced. Off disables it.
FLAC_CACHE_ENABLED = _env_bool("FLAC_CACHE_ENABLED", True)

# Cache the repair scan's Qobuz ISRC→track lookups on disk
# (DATA_DIR/repair_cache.db), so a re-scan, or any album sharing an ISRC,
# skips the network round trip. The files themselves are still decode-tested
# fresh every scan, so this never hides on-disk corruption; only the lookup is
# cached. Entries re-verify against Qobuz after REPAIR_CACHE_TTL_DAYS (0 keeps
# them until the db is deleted). Off disables it.
REPAIR_CACHE_ENABLED = _env_bool("REPAIR_CACHE_ENABLED", True)
REPAIR_CACHE_TTL_DAYS = _env_num_min("REPAIR_CACHE_TTL_DAYS", 30, 0)

# How long (seconds) a cached artist catalog (get_artist_albums) is reused before
# re-fetching. Catalogs change only when an artist puts out something new, so a
# few days keeps frequent re-scans free while still surfacing new releases. 0
# disables catalog caching (album caching, being immutable, stays on). Gated by
# ALBUM_CACHE_ENABLED.
ARTIST_CATALOG_CACHE_TTL = _env("ARTIST_CATALOG_CACHE_TTL", 7 * 86400)

# How often (seconds) the dashboard quietly runs the new-release check when you
# open the app, so new albums surface on their own. Throttled to this interval
# and skipped while anything's already scanning; it only ever parks a review
# list (never auto-downloads). 0 turns the automatic check off; the manual
# "Check for new releases" buttons still work.
NEW_RELEASE_CHECK_INTERVAL = _env("NEW_RELEASE_CHECK_INTERVAL", 86400)

# A "new release" should actually be recent. Qobuz routinely back-fills older
# catalogue titles, so without a recency window an album from years ago that it
# only just listed gets flagged "new". That is a back-catalogue gap, not a new
# release. Only surface albums whose ORIGINAL release date is within this many
# days. The baseline still records suppressed albums, so they never resurface
# here. 0 disables the window (any catalog newcomer counts, the old behaviour).
# Gap-fill (Library / Artist "missing albums") is unaffected. It deliberately
# surfaces old albums you don't own; this only narrows the new-release check.
NEW_RELEASE_MAX_AGE_DAYS = _env("NEW_RELEASE_MAX_AGE_DAYS", 365)

# Resume an interrupted library scan when the app is idle, driving it to
# completion across restarts so the new-release baseline gets established
# without the user re-triggering it. A fresh first scan is never auto-started;
# it's offered as a choice on first run. Off disables both that first-run offer
# and the automatic resume; the manual Resume button works regardless.
AUTO_LIBRARY_SCAN = _env_bool("AUTO_LIBRARY_SCAN", True)

# Per-request budgets for the web UI's Qobuz API calls (album/search/track
# fetches and the Settings token check). A slow Qobuz response shouldn't
# park a worker thread for minutes, so the user gets a clear timeout instead.
WEB_FETCH_TIMEOUT     = _env_num_min("QL_WEB_FETCH_TIMEOUT",     12.0, 1.0)
WEB_TEST_AUTH_TIMEOUT = _env_num_min("QL_WEB_TEST_AUTH_TIMEOUT", 8.0,  1.0)

# Web job log and SSE tunables.
# JOB_LOG_CAP is the per-job line ceiling; very long artist walks
# (500+ albums) on tight memory should drop this to e.g. 2000.
# JOB_LOG_REPLAY_TAIL is what a late SSE subscriber gets replayed; 0 disables
# historical replay while keeping live lines and the terminal event.
# POST_JOB_HOOK_TIMEOUT bounds the subprocess that fires the optional
# post-job hook. Slow webhooks (Apprise, ntfy with retries) may need
# more. SSE_MAX_WORKERS sets the thread pool for SSE streams (each
# active subscriber holds one). SSE_HEARTBEAT_TICKS sets how many
# 0.5s queue-empty ticks pass before a `: ping` keepalive. Reverse
# proxies with short idle timeouts (60s default on most) want a lower
# value.
JOB_LOG_CAP          = _env_num_min("JOB_LOG_CAP",         5000, 1)
JOB_LOG_REPLAY_TAIL  = _env_num_min("JOB_LOG_REPLAY_TAIL",  500, 0)
# Ceiling on candidates a single review job holds in memory (and persists/
# rehydrates). A whole-library gap scan on a very large collection could
# otherwise grow this unbounded; past the cap the scan stops adding and the
# summary says so, so the box stays safe and the user can narrow or raise it.
# The default is high enough that a realistic whole-library scan is never
# silently truncated (a dense ~2k-artist library produced ~37k gaps). It is a
# runaway guard, not a routine limit.
JOB_CANDIDATE_CAP    = _env_num_min("JOB_CANDIDATE_CAP",  100000, 1)
POST_JOB_HOOK_TIMEOUT = _env_num_min("POST_JOB_HOOK_TIMEOUT", 10, 1)
SSE_MAX_WORKERS      = _env_num_min("SSE_MAX_WORKERS", 16, 1)
SSE_HEARTBEAT_TICKS  = _env_num_min("SSE_HEARTBEAT_TICKS", 30, 1)

# ── Fuzzy-match thresholds ────────────────────────────────────────────────────
FUZZY_DIR_THRESH           = _env_unit_float("FUZZY_DIR_THRESH",           0.78)
FUZZY_DIR_MIN_COVERAGE     = _env_unit_float("FUZZY_DIR_MIN_COVERAGE",     0.75)
CONSOLIDATE_THRESH         = _env_unit_float("CONSOLIDATE_THRESH",         0.70)
ARTIST_NAME_THRESH         = _env_unit_float("ARTIST_NAME_THRESH",         0.85)
ARTIST_DIR_MATCH_THRESH    = _env_unit_float("ARTIST_DIR_MATCH_THRESH",    0.65)
AUTO_SAFE_TITLE_SIM_THRESH = _env_unit_float("AUTO_SAFE_TITLE_SIM_THRESH", 0.85)

# ── Catalog / walk ────────────────────────────────────────────────────────────
EDITION_SEARCH_API_BUDGET = _env("EDITION_SEARCH_API_BUDGET", 3)

LEFTOVER_WARN_LIMIT    = _env("LEFTOVER_WARN_LIMIT",    50)
# Max albums fetched per artist when scanning. 500 truncated prolific artists
# (the Beatles list ~900 on Qobuz), so a gap scan missed albums; 1000 covers
# realistic discographies. Truncation past this is logged by the scanner.
ARTIST_CATALOG_LIMIT   = _env_num_min("ARTIST_CATALOG_LIMIT",   1000, 1)
ARTIST_CATALOG_PAGE    = _env_num_min("ARTIST_CATALOG_PAGE",    100, 1)
# Hide releases shorter than this in the "missing albums" step of artist
# mode (singles/small EPs are usually noise; lower it to 1 to surface them).
MISSING_ALBUMS_MIN_TRACKS = _env("MISSING_ALBUMS_MIN_TRACKS", 4)

# ── Retention windows (days) ──────────────────────────────────────────────────
UPGRADE_BACKUP_RETENTION_DAYS = _env_num_min("UPGRADE_BACKUP_RETENTION_DAYS", 7, 0)
CAPPED_RETENTION_DAYS         = _env_num_min("CAPPED_RETENTION_DAYS",         90, 0)

# ── Feature flags ─────────────────────────────────────────────────────────────
UPGRADE_SCAN_ENABLED = _env_bool("UPGRADE_SCAN_ENABLED", True)
AUTO_UPGRADE_ENABLED = _env_bool("AUTO_UPGRADE_ENABLED", False)
# Whether the Upgrade walk re-rips tracks you grabbed as singles. Off by
# default: a grabbed single is a deliberate one-off, not part of a collection
# you're keeping at best quality, so the walk leaves it alone unless you opt in.
UPGRADE_SINGLES_ENABLED = _env_bool("UPGRADE_SINGLES_ENABLED", False)
# Off by default: grabbing one song should not make the rest of that album
# disappear from future gap scans. Turn this on only if one-track grabs are
# intended to be permanent singles.
SUPPRESS_SINGLE_TRACK_GAPS = _env_bool("SUPPRESS_SINGLE_TRACK_GAPS", False)
# Off by default: most people want the file Qobuz delivers. Opt in if you
# prefer to grab hi-res mixes and downsample them to 44.1/48 kHz to save space.
DOWNSAMPLE_HIRES_ENABLED = _env_bool("DOWNSAMPLE_HIRES_ENABLED", False)
# Off by default: downsampling exists to reclaim space, and the copies cost
# that space back while they last. On, the on-demand Downsample parks each
# hi-res original in the backup area for UPGRADE_BACKUP_RETENTION_DAYS, so
# the rewrite can be undone from Settings until the window closes.
DOWNSAMPLE_KEEP_ORIGINALS = _env_downsample_choice()

# Album-version / library-structure preferences. CLI and web both read these,
# so shared options behave the same across interfaces.
#   PREFER_HIRES         pick the hi-res master when an album has several versions
#   CONSOLIDATE          after import, merge sibling/duplicate album folders
#   MIGRATE_MULTI_ARTIST move "Primary, Other/<album>" into "Primary/<album>"
# CONSOLIDATE defaults off: it moves/merges folders, which is opinionated for
# someone else's library layout. It's CLI-only and prompts per folder anyway;
# turn it on via env or CLI if you want it.
PREFER_HIRES         = _env_bool("PREFER_HIRES",         True)
CONSOLIDATE          = _env_bool("CONSOLIDATE",          False)
MIGRATE_MULTI_ARTIST = _env_bool("MIGRATE_MULTI_ARTIST", False)

# Drop obvious live/tour/session/acoustic releases from the "missing albums"
# gap list. Off by default so behaviour is unchanged: many people do want the
# live records, and the guard is conservative (it only fires on a clearly
# tagged release, never on an album whose real title merely contains "Live").
EXCLUDE_LIVE_ALBUMS = _env_bool("EXCLUDE_LIVE_ALBUMS", False)

# ── Audio extensions ──────────────────────────────────────────────────────────
AUDIO_EXTS = (".flac", ".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wav")
