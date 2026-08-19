"""Persisted behaviour settings for the web UI, layered on top of config.py.

Values are applied to the config module so flows read cfg.* at call time.
During a running job the in-memory apply is deferred until the worker idles
to avoid mid-job quality or config changes; disk write still happens immediately.
"""
import logging
import threading
from typing import Optional

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.ui_cli.errors import plural

log = logging.getLogger("qobuz_librarian")

SETTINGS_FILE = cfg.DATA_DIR / ".qobuz_settings.json"

# Set by save() when an active job blocks the in-memory apply; drained by
# drain_pending() once the worker idles.
_pending_apply: Optional[dict] = None
_pending_lock = threading.Lock()

# (key, label, help) - display order on the Settings page.
BEHAVIOR_FIELDS = [
    ("PREFER_HIRES", "Prefer hi-res editions",
     "When several editions are available, choose the highest quality allowed "
     "by your download quality setting. Turn this off to favour the original "
     "edition."),
    ("MIGRATE_MULTI_ARTIST", "Migrate multi-artist folders",
     "After import, file albums credited to multiple artists under the primary "
     "album artist instead of a combined artist folder."),
    ("DOWNSAMPLE_HIRES_ENABLED", "Downsample new hi-res downloads",
     "Before import, reduce newly downloaded hi-res FLACs to 44.1 or 48 kHz. "
     "The hi-res version is not kept. The Downsample page has its own keep "
     "setting."),
    ("SUPPRESS_SINGLE_TRACK_GAPS", "Treat track downloads as singles",
     "Hide the rest of an album from gap scans after you download one track. "
     "Leave this off if a track download should not affect future album offers."),
    ("LYRICS_ENABLED", "Fetch lyrics",
     "Fetch lyrics during import, using synced lyrics when providers have them."),
]
BEHAVIOR_KEYS = [k for k, _, _ in BEHAVIOR_FIELDS]

# Provider names lyric_fetch.py knows how to drive (mirrors the provider list
# it wires up).
LYRICS_PROVIDER_CHOICES = [
    "Lrclib", "NetEase", "Megalobiz", "Musixmatch", "Genius",
]

# (key, label, help, kind, choices, placeholder).
TEXT_FIELDS = [
    ("STREAMRIP_QUALITY", "Download quality",
     "Maximum quality to request. If Qobuz serves less than expected, "
     "Qobuz Librarian retries at the highest tier and keeps the saved file "
     "aligned with this setting.",
     "enum", ["4", "3", "2"], ""),
    ("LYRICS_FORMAT", "Lyrics format",
     "How lyrics are written when fetched.",
     "enum", ["embed", "sidecar", "both"], ""),
    ("ARTWORK", "Album art",
     "Where cover art goes: a file in the album folder (sidecar), embedded in "
     "the track tags with no leftover file (embed), or both.",
     "enum", ["sidecar", "embed", "both"], ""),
    ("LYRICS_PROVIDERS", "Lyrics providers",
     "Comma-separated list of providers to try in order. Available: Lrclib, "
     "NetEase, Megalobiz, Musixmatch, Genius. Unknown names are ignored. "
     "Empty tries them in the provider library's own order.",
     "list", LYRICS_PROVIDER_CHOICES, "e.g. Lrclib, NetEase"),
    ("LASTFM_API_KEY", "Last.fm API key",
     "Empty hides the Discover tab.",
     "text", None, "32-character key"),
    ("COLLECTION_BACKUP_DIR", "Backup folder",
     "Leave empty to keep it with the app's other data.",
     "text", None, "e.g. /data/collection-backups"),
    ("BEETS_PATH_DEFAULT", "beets path: default",
     "Folder/file naming for normal albums (beets path syntax). "
     "Empty = use beets/config.yaml.",
     "text", None, "e.g. $albumartist/$album ($year)/$track - $title"),
    ("BEETS_PATH_SINGLETON", "beets path: singleton",
     "Naming for singleton tracks. Empty = beets default.",
     "text", None, "e.g. $albumartist/$album ($year)/$track - $title"),
    ("BEETS_PATH_COMP", "beets path: compilation",
     "Naming for compilations / Various Artists. Empty = beets default.",
     "text", None, "e.g. Various Artists/$album ($year)/$track - $title"),
    ("BEETS_PLUGINS", "beets plugins",
     "beets plugins to enable (replaces the config.yaml list). "
     "Empty = use that file. Unknown names are dropped and flagged. "
     "e.g. lastgenre, replaygain, scrub, edit.",
     "list", None, "fetchart,lastgenre,replaygain"),
    ("ARTIST_CATALOG_CACHE_TTL", "Album-list freshness",
     "How long gap scans reuse a fetched discography before refetching. "
     "(New-release checks always fetch fresh.)",
     "enum", ["86400", "259200", "604800", "2592000"], ""),
    ("NEW_RELEASE_CHECK_INTERVAL", "Auto-check for new releases",
     "How often to auto-check for new releases on app open. Results go to the "
     "dashboard to review; nothing downloads. Off = manual only.",
     "enum", ["0", "21600", "43200", "86400", "604800"], ""),
    ("DOWNSAMPLE_KEEP_ORIGINALS", "Keep originals when downsampling",
     "Whether the Downsample page parks a restorable copy of each hi-res "
     "original before rewriting it, or deletes it to save space.",
     "enum", ["keep", "delete"], ""),
]
TEXT_KEYS = [k for k, *_ in TEXT_FIELDS]
# Account secrets among the text fields. Masked in the form and never quoted
# back in a warning, the way the Qobuz token already is.
SECRET_KEYS = frozenset({"LASTFM_API_KEY"})

# What the environment (Compose, .env) supplies for each of those fields,
# captured at import - before load() layers the saved settings over cfg. A
# field the user empties goes back to tracking this value, so it has to be
# kept somewhere the save path can still read it.
_ENV_DEFAULTS = {key: getattr(cfg, key, "") for key, *_ in TEXT_FIELDS}

# Enum fields whose value is an int on cfg (the form/JSON carry strings).
_INT_ENUM_KEYS = {"STREAMRIP_QUALITY", "ARTIST_CATALOG_CACHE_TTL",
                  "NEW_RELEASE_CHECK_INTERVAL"}

# Enum fields with a real unset state. The keep-or-delete answer starts unset
# and the first downsample asks for it, so Settings has to offer that state
# back as a choice, not only show it until something else is picked.
UNSET_ENUM_KEYS = ("DOWNSAMPLE_KEEP_ORIGINALS",)

# Friendlier dropdown text for enum values whose bare value isn't self-explaining;
# falls back to the raw value for anything not listed.
ENUM_OPTION_LABELS = {
    "STREAMRIP_QUALITY": {
        "4": "24-bit ≤192 kHz",
        "3": "24-bit ≤96 kHz",
        "2": "16-bit / 44.1 kHz",
    },
    "LYRICS_FORMAT": {
        "embed": "Embed in tags",
        "sidecar": "Sidecar .lrc files",
        "both": "Embed and sidecar",
    },
    "ARTWORK": {
        "sidecar": "Cover file",
        "embed": "Embed in tags",
        "both": "Cover file and tags",
    },
    "ARTIST_CATALOG_CACHE_TTL": {
        "86400": "1 day",
        "259200": "3 days",
        "604800": "7 days (default)",
        "2592000": "30 days",
    },
    "NEW_RELEASE_CHECK_INTERVAL": {
        "0": "Off",
        "21600": "Every 6 hours",
        "43200": "Every 12 hours",
        "86400": "Daily (default)",
        "604800": "Weekly",
    },
    "DOWNSAMPLE_KEEP_ORIGINALS": {
        "keep": "Keep a restorable backup",
        "delete": "Delete to save space",
    },
}


def inert_behaviour_notes(values) -> dict:
    """What a saved combination has switched off, keyed by the setting it hits.

    Download quality is the cap streamrip is invoked with, so at 16-bit /
    44.1 kHz nothing can arrive above CD rate. That leaves the downsample
    toggle on screen with nothing to act on, and hi-res edition picking still
    deciding the edition but no longer the quality. Neither is refused, so the
    page has to say which one still does something.
    """
    notes = {}
    if str(values.get("STREAMRIP_QUALITY", "")) != "2":
        return notes
    quality = ENUM_OPTION_LABELS["STREAMRIP_QUALITY"]["2"]
    if values.get("DOWNSAMPLE_HIRES_ENABLED"):
        notes["DOWNSAMPLE_HIRES_ENABLED"] = (
            f"Your download quality is {quality}, so nothing arrives above CD "
            "rate and this never runs.")
    if values.get("PREFER_HIRES"):
        notes["PREFER_HIRES"] = (
            f"At {quality} this only picks the edition.")
    return notes


def _list_to_str(v) -> str:
    if isinstance(v, list):
        return ",".join(v)
    return str(v or "")


def _field_str(v, kind) -> str:
    """Stringify a setting for the form/template. int-valued enums keep 0 as
    "0" (a real choice, e.g. the auto-check's Off) rather than collapsing it to
    the empty string the `v or ""` shortcut would produce."""
    if kind == "list":
        return _list_to_str(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return str(v or "")


def _str_to_list(s: str) -> list:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def _normalize_list_choices(items, choices):
    """Keep only entries naming a known choice, normalised to its canonical
    spelling (case-insensitive). Drops unknowns and de-dupes, preserving order.
    Returns (kept, dropped)."""
    canon = {c.lower(): c for c in choices}
    kept, dropped = [], []
    for it in items:
        c = canon.get(str(it).strip().lower())
        if c is None:
            dropped.append(str(it).strip())
        elif c not in kept:
            kept.append(c)
    return kept, dropped


def _available_beets_plugins():
    """Plugin names beets can actually import on this server (submodules of the
    beetsplug namespace). Returns None when the set can't be determined - beets
    isn't importable from here, or enumeration failed - so the caller validates
    nothing rather than dropping every plugin the user typed."""
    try:
        import pkgutil

        import beetsplug
    except Exception:
        return None
    try:
        names = {m.name for m in pkgutil.iter_modules(beetsplug.__path__)
                 if not m.name.startswith("_")}
    except Exception:
        return None
    return names or None


def _validate_list(key, items):
    """Sanitise a comma-separated list setting. Returns (kept, dropped).

    LYRICS_PROVIDERS is matched against the providers lyric_fetch can drive;
    BEETS_PLUGINS against the plugins beets can actually load here - a name
    that can't load would otherwise break every import silently. Both
    normalise spelling case-insensitively, drop unknowns, and de-dupe. Fields
    with no known set (free-text lists) just de-dupe."""
    if key == "LYRICS_PROVIDERS":
        return _normalize_list_choices(items, LYRICS_PROVIDER_CHOICES)
    if key == "BEETS_PLUGINS":
        avail = _available_beets_plugins()
        if avail:
            return _normalize_list_choices(items, sorted(avail))
        return list(dict.fromkeys(i.strip() for i in items if i.strip())), []
    return list(dict.fromkeys(i.strip() for i in items if i.strip())), []


_PATH_TEMPLATE_KEYS = ("BEETS_PATH_DEFAULT", "BEETS_PATH_SINGLETON",
                       "BEETS_PATH_COMP")
_FIELD_LABELS = {key: label for key, label, *_ in TEXT_FIELDS}
_FIELD_KINDS = {key: kind for key, _, _, kind, _, _ in TEXT_FIELDS}


def _path_template_problem(key, value):
    """Why a beets path template cannot be saved, in the user's words, or ""
    when it is fine. The rule itself lives with the beets config writer, which
    applies it to environment-supplied templates too."""
    if key not in _PATH_TEMPLATE_KEYS or not value:
        return ""
    from qobuz_librarian.integrations.beets import unsafe_path_template

    reason = unsafe_path_template(value)
    if not reason:
        return ""
    label = _FIELD_LABELS.get(key, key)
    if reason == "absolute":
        return (f"\u201c{label}\u201d has to be relative to your music folder. "
                "Remove what comes before the first folder name so albums land "
                "inside it.")
    return (f"\u201c{label}\u201d has to stay inside your music folder. "
            "Remove the \u201c..\u201d parts.")


def _restored_default_warning(key, value):
    """Said when emptying a field hands it back to a value the environment
    supplies, so the box refilling itself doesn't read as a save that failed.

    A secret field is named, never quoted: the point is that the box refilled
    itself, and printing the value would put the key into the page in clear.
    """
    label = _FIELD_LABELS.get(key, key)
    restored = "the key it supplies" if key in SECRET_KEYS else value
    return (f"\u201c{label}\u201d comes from your Compose file, so clearing the "
            f"box put {restored} back. Remove {key} there to stop using it.")


def _dropped_warning(key, dropped):
    names = ", ".join(dropped)
    if key == "LYRICS_PROVIDERS":
        return (f"Ignored {plural(len(dropped), 'unrecognised lyrics provider')}: "
                f"{names}. "
                f"Known providers are {', '.join(LYRICS_PROVIDER_CHOICES)}.")
    if key == "BEETS_PLUGINS":
        return (f"Ignored {plural(len(dropped), 'beets plugin')} not installed "
                f"on this server: {names}. "
                "Check the spelling, or install them and add them back.")
    return f"Ignored {plural(len(dropped), 'unrecognised value')} for {key}: {names}."


def current() -> dict:
    """Live value of every persisted setting, as seen by the cfg module.

    If a save() was deferred while a job was running, the pending values
    are overlaid so the Settings page reflects what the user saved rather
    than the still-unchanged cfg.* values. The cfg.* read happens inside
    _pending_lock so a concurrent drain_pending (which holds the lock
    across its _apply) can't slip cfg.* updates in between the cfg read
    and the overlay read - that gap silently dropped the pending change.
    """
    with _pending_lock:
        out = {k: bool(getattr(cfg, k)) for k in BEHAVIOR_KEYS}
        for key, _, _, kind, _, _ in TEXT_FIELDS:
            out[key] = _field_str(getattr(cfg, key, ""), kind)
        if _pending_apply:
            for k in BEHAVIOR_KEYS:
                if k in _pending_apply:
                    out[k] = bool(_pending_apply[k])
            for key, _, _, kind, _, _ in TEXT_FIELDS:
                if key in _pending_apply:
                    out[key] = _field_str(_pending_apply[key], kind)
    return out


def _apply(values: dict):
    for k in BEHAVIOR_KEYS:
        if k in values:
            setattr(cfg, k, bool(values[k]))
    for key, _, _, kind, choices, _ in TEXT_FIELDS:
        if key not in values:
            continue
        raw = values[key]
        if kind == "list":
            items = _str_to_list(raw) if isinstance(raw, str) else list(raw or [])
            items, _ = _validate_list(key, items)
            setattr(cfg, key, items)
        elif kind == "enum":
            v = str(raw or "").strip().lower()
            if not v and key in UNSET_ENUM_KEYS:
                setattr(cfg, key, None)
                continue
            if key == "STREAMRIP_QUALITY" and v in ("0", "1"):
                # 0/1 are lossy MP3 tiers the FLAC-only pipeline discards.
                v = "2"
            if choices and v not in choices:
                continue  # ignore garbage, keep current
            # A few enums are ints on cfg (quality tier, cache seconds) - keep
            # the type stable so cfg.* stays int, not str, on the post-save path.
            if key in _INT_ENUM_KEYS:
                try:
                    setattr(cfg, key, int(v))
                except ValueError:
                    continue
            else:
                setattr(cfg, key, v)
        else:
            setattr(cfg, key, str(raw or "").strip())


def _read_settings():
    """The persisted settings, or None when there are none to read. A corrupt
    file is kept aside instead of being flattened by the next save - every
    behaviour setting would otherwise revert to its env default silently."""
    return state_file.load_json_object(
        SETTINGS_FILE, "the settings file", "your saved Settings")


def _blank_overrides(data) -> list:
    """Keys in a saved settings dict that hold nothing. An empty text or list
    field means "use whatever the environment gives me", which is what having
    no key at all does, so a blank one is only ever left over from a save that
    wrote it. Keeping it would hide the Compose value for good. Most enums are
    excluded, having no empty choice to make; the ones in UNSET_ENUM_KEYS do
    have one and clear the same way."""
    return [key for key, _, _, kind, _, _ in TEXT_FIELDS
            if (kind != "enum" or key in UNSET_ENUM_KEYS) and key in data
            and not str(data[key] or "").strip()]


def _normalise(data) -> bool:
    """Correct a saved settings dict in place. True when something changed."""
    changed = False
    # A persisted lossy STREAMRIP_QUALITY (0/1) is a tier the FLAC pipeline
    # discards; _apply already coerces it to 2 in cfg.
    if str(data.get("STREAMRIP_QUALITY", "")).strip() in ("0", "1"):
        data["STREAMRIP_QUALITY"] = "2"
        changed = True
    for key in _blank_overrides(data):
        del data[key]
        changed = True
    return changed


def load():
    """Apply the persisted settings file over env defaults, if present."""
    data = _read_settings()
    if data is None:
        return
    stale = _normalise(data)
    _apply(data)
    # Correct the file too, so a value this loader had to fix doesn't linger
    # and get fixed again on every start. Under the store lock: this is a
    # boot-time read-modify-write and the other process may be mid-save.
    if stale:
        with state_file.store_lock(SETTINGS_FILE):
            fresh = _read_settings()
            if fresh is not None and _normalise(fresh):
                _atomic_write_settings(fresh)


def _any_active_job() -> bool:
    """True if a job is genuinely in flight (pending/scanning/running). Parked
    reviews don't count: they sit for weeks by design, and deferring saves
    under them means changes that never land while the settings page shows the
    new values as current.
    """
    try:
        from qobuz_librarian.web import jobs as job_mgr
    except ImportError:
        return False
    return any(j.status != job_mgr.JobStatus.AWAITING_REVIEW
               for j in job_mgr.registry.pending_and_running())


def drain_pending():
    """Apply any settings change that was deferred because a job was
    running. Called by the worker loop after each task completes.

    Holds _pending_lock across _apply so a concurrent save()'s `current()`
    read either sees the overlay AND blocks on the lock, or sees the
    already-applied cfg.* values - never the gap in between, which would
    silently drop the pending change.
    """
    from qobuz_librarian.web import jobs as job_mgr

    global _pending_apply
    with job_mgr.settings_admission_guard(), _pending_lock:
        pending = _pending_apply
        _pending_apply = None
        if pending is not None:
            _apply(pending)


_save_lock = threading.Lock()


def _atomic_write_settings(data: dict) -> bool:
    """Persist the settings dict to SETTINGS_FILE atomically (temp + os.replace).
    Returns True on success, False on any OSError."""
    try:
        state_file.write_json(SETTINGS_FILE, data)
        return True
    except OSError:
        return False


def save(values: dict):
    """Apply settings and persist them atomically. Returns (ok, warnings),
    where ok is True on success, False on persistence failure, and None when
    the submitted settings are invalid. Only
    real changes land in the settings file: a posted value that matches what's
    already in effect - and was never saved before - stays out, so that field
    keeps tracking its env var / default instead of being silently pinned
    forever by an unrelated Settings save.
    """
    # The CLI writes this store too (the downsample walk saves the
    # keep-originals choice while the web stays browsable in terminal mode),
    # so the thread lock alone can't serialise the read-merge-write.
    with _save_lock, state_file.store_lock(SETTINGS_FILE):
        return _save_locked(values)


def _save_locked(values: dict):
    # What the user currently sees (cfg overlaid with any deferred save) - the
    # baseline that decides whether a posted value is actually a change.
    baseline = current()
    clean = {}
    warnings = []
    for k in BEHAVIOR_KEYS:
        if k in values:
            clean[k] = bool(values[k])
    for key, _, _, kind, choices, _ in TEXT_FIELDS:
        if key not in values:
            continue
        # Enum/list values are validated here too (not just in _apply), so a
        # forged POST can't persist a value the loader would reject - the
        # on-disk file stays consistent with cfg.
        if kind == "enum" and choices:
            v = str(values[key] or "").strip().lower()
            if not v and key in UNSET_ENUM_KEYS:
                # Unset is an answer here, not a missing one: the first
                # downsample asks for this choice, and the form posts an empty
                # value both when nothing has been chosen yet and when the user
                # picks that entry again. It clears below like an emptied box.
                clean[key] = ""
                continue
            if key == "STREAMRIP_QUALITY" and v in ("0", "1"):
                v = "2"  # lossy tiers the FLAC pipeline discards (see _apply)
            if v not in choices:
                # Reject the whole submitted change. Applying valid sibling
                # fields while silently dropping a forged/invalid enum leaves
                # the operator with a configuration they never submitted.
                return None, [f"Invalid value for {key}."]
            clean[key] = v  # persist the normalised value, matching cfg
        elif kind == "list":
            raw = values[key]
            items = _str_to_list(raw) if isinstance(raw, str) else list(raw or [])
            kept, dropped = _validate_list(key, items)
            clean[key] = ",".join(kept)
            if dropped:
                warnings.append(_dropped_warning(key, dropped))
        else:
            # Match _apply's strip so an incidental trailing space doesn't
            # read as a change and pin the field.
            value = str(values[key] or "").strip()
            problem = _path_template_problem(key, value)
            if problem:
                # Same rule as the enum branch: reject the whole submission
                # rather than apply the siblings and leave a path that would
                # quietly file music outside the library.
                return None, [problem]
            clean[key] = value

    persisted = _read_settings() or {}
    cleared = _blank_overrides(clean)
    for k, v in clean.items():
        if k in cleared:
            # Emptied: the field goes back to tracking whatever the environment
            # supplies, or the beets config file when it supplies nothing. That
            # is what carrying no key means, so the key leaves the store rather
            # than pinning a blank over the Compose value for good.
            persisted.pop(k, None)
        elif k in persisted or v != baseline.get(k):
            persisted[k] = v

    # cfg has to follow in this session, not only after the next restart.
    applied = dict(clean)
    for k in cleared:
        applied[k] = _ENV_DEFAULTS.get(k, "")
        shown = _field_str(applied[k], _FIELD_KINDS.get(k, "text"))
        if shown:
            warnings.append(_restored_default_warning(k, shown))

    from qobuz_librarian.web import jobs as job_mgr

    with job_mgr.settings_admission_guard(), _pending_lock:
        # Durable publication is the admission point for a settings change.
        # Applying first leaves the running process (or its deferred overlay)
        # claiming values that a restart will discard when the atomic write
        # fails. Keep the exact previous live and pending state on failure.
        if not _atomic_write_settings(persisted):
            return False, warnings
        global _pending_apply
        if _any_active_job():
            # Merge onto any change still waiting, so this save can't drop an
            # earlier deferred one.
            _pending_apply = {**(_pending_apply or {}), **applied}
        else:
            # Fold in any deferred change and clear the slot under the lock,
            # so a drain firing right after can't roll these fields back to
            # the old deferred copy.
            merged = {**(_pending_apply or {}), **applied}
            _pending_apply = None
            _apply(merged)

    return True, warnings
