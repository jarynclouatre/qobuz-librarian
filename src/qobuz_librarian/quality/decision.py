"""Quality comparison helpers and upgrade detection."""
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import state_file
from qobuz_librarian.api.auth import AuthLost, QobuzError
from qobuz_librarian.api.search import get_artist_albums, search_artists
from qobuz_librarian.library import hidden as hidden_mod
from qobuz_librarian.library.catalog import (
    album_quality_label,
    album_year,
    compute_missing,
    find_existing_tracks,
    find_extras_in_existing,
    is_lossless_album,
)
from qobuz_librarian.library.scanner import (
    drop_artist_subdirs_cache,
    list_artist_album_dirs,
)
from qobuz_librarian.library.tags import similarity, strip_album_decorations
from qobuz_librarian.quality.tiers import streamrip_quality_cap
from qobuz_librarian.ui_cli.logging import vlog


def album_max_quality(qobuz_album, tier=None):
    """Return the (bit_depth, sample_rate_hz) Qobuz can provide, capped to the
    streamrip quality tier.

    Qobuz reports sample rate in kHz (e.g. 96.0); values >= 1000 are treated
    as already-Hz so an API change to Hz can't make every album read as
    'lower than Qobuz' and trigger spurious upgrades.
    """
    bd = qobuz_album.get("maximum_bit_depth") or 0
    sr = qobuz_album.get("maximum_sampling_rate") or 0
    sr_hz = int(round(sr)) if sr >= 1000 else int(round(sr * 1000))
    cap_bd, cap_sr = streamrip_quality_cap(tier)
    bd = min(bd, cap_bd) if bd else 0
    sr_hz = min(sr_hz, cap_sr) if sr_hz else 0
    return (bd, sr_hz)


def existing_track_quality(track):
    """Return (bit_depth, sample_rate_hz) for an existing track dict.
    Mutagen's FLAC.info gives sample_rate in Hz directly; bits_per_sample is
    bits (16, 24)."""
    return (track.get("bits") or 0, track.get("sample_rate") or 0)


def _known_positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def quality_relation(left, right):
    """Return how ``left`` relates to ``right`` without ranking tradeoffs.

    Bit depth and sample rate are independent quality dimensions.  A format is
    higher only when neither dimension falls, lower only when neither rises,
    and incomparable when one rises while the other falls.  Missing or partial
    measurements cannot authorize an automatic replacement.
    """
    left_bits = _known_positive_int(left[0])
    left_rate = _known_positive_int(left[1])
    right_bits = _known_positive_int(right[0])
    right_rate = _known_positive_int(right[1])
    if None in (left_bits, left_rate, right_bits, right_rate):
        return "unknown"
    if left_bits == right_bits and left_rate == right_rate:
        return "equal"
    if left_bits >= right_bits and left_rate >= right_rate:
        return "higher"
    if left_bits <= right_bits and left_rate <= right_rate:
        return "lower"
    return "incomparable"


def compare_album_quality(existing_tracks, qobuz_album):
    """Classify per-track quality of existing tracks vs the Qobuz album. Returns
    dict with classification (str) and counts. Classifications: 'no_existing'
    : existing list is empty 'unknown' : couldn't read quality from any
    existing track 'all_lower' : every readable track is below Qobuz quality
    'mixed_below' : some below, some at; none above 'all_equal' : every
    readable track matches Qobuz quality 'all_higher' : every readable track
    is above Qobuz quality 'mixed_above' : some above, some at; none below
    'mixed_both' : some below AND some above (rare; treat conservatively)
    'incomparable': at least one track trades bit depth for sample rate A
    result containing unknown or incomparable quality is never classified as
    an automatic upgrade candidate.
    """
    qbits, qrate = album_max_quality(qobuz_album)
    n_below = n_at = n_above = n_unknown = n_incomparable = 0

    for t in existing_tracks:
        ebits, erate = existing_track_quality(t)
        relation = quality_relation((ebits, erate), (qbits, qrate))
        if relation == "unknown":
            n_unknown += 1
        elif relation == "lower":
            n_below += 1
        elif relation == "higher":
            n_above += 1
        elif relation == "equal":
            n_at += 1
        else:
            n_incomparable += 1

    n_known = n_below + n_at + n_above
    if not existing_tracks:
        cls = "no_existing"
    elif n_unknown:
        cls = "unknown"
    elif n_incomparable:
        cls = "incomparable"
    elif n_known == 0:
        cls = "unknown"
    elif n_below > 0 and n_above > 0:
        cls = "mixed_both"
    elif n_below > 0:
        cls = "all_lower" if n_at == 0 else "mixed_below"
    elif n_above > 0:
        cls = "all_higher" if n_at == 0 else "mixed_above"
    else:
        cls = "all_equal"

    return {
        "classification": cls,
        "qobuz_quality": (qbits, qrate),
        "n_below": n_below,
        "n_at": n_at,
        "n_above": n_above,
        "n_unknown": n_unknown,
        "n_incomparable": n_incomparable,
    }


def _track_quality_cmp(t1, t2):
    """Compare audio quality of two track dicts.
    Returns 1 if t1 > t2, -1 if t1 < t2, 0 if equal, and None when
    incomplete/crossed measurements or a channel-count change make the
    comparison unsafe.
    """
    q1 = (t1.get("bits") or 0, t1.get("sample_rate") or 0)
    q2 = (t2.get("bits") or 0, t2.get("sample_rate") or 0)
    relation = quality_relation(q1, q2)
    c1 = _known_positive_int(t1.get("channels"))
    c2 = _known_positive_int(t2.get("channels"))
    if relation in ("unknown", "incomparable") or c1 is None or c2 is None:
        return None
    if c1 != c2:
        return None
    return {"higher": 1, "lower": -1, "equal": 0}[relation]


def quality_change_summary(overlap):
    """Count tracks that would be a downgrade if deleted. A sibling track whose
    own quality can't be read - a FLAC with no usable STREAMINFO/title falls back
    to (0, 0) - is counted as ``unknown``: it could be hi-res, so deleting it must
    clear the same confirmation as a known hi-res loss rather than reading as a
    safe lower-quality drop."""
    losing_hires = same = upgrading = unknown = 0
    for st, pt in overlap:
        cmp = _track_quality_cmp(st, pt)
        if cmp is None:
            unknown += 1
            continue
        if cmp > 0:
            losing_hires += 1
        elif cmp < 0:
            upgrading += 1
        else:
            same += 1
    return {"losing_hires": losing_hires, "same": same,
            "upgrading": upgrading, "unknown": unknown}


# ── Upgrade-cap persistence ───────────────────────────────────────────────────

def load_capped():
    # A malformed file that parses as a list/string would otherwise reach
    # is_album_capped's `.get` and crash the upgrade scan.
    data = state_file.load_json_object(
        cfg.CAPPED_FILE, "the upgrade-cap store",
        "the record of albums already at their best quality, and the ones you "
        "chose to keep downsampled")
    return data if data is not None else {}


def _capped_ts(entry):
    # Parse an entry's "ts" into a tz-aware datetime, or None if missing/bad.
    try:
        ts = datetime.fromisoformat((entry or {}).get("ts", ""))
    except (ValueError, TypeError, AttributeError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _capped_is_fresh(ts):
    # Single staleness boundary shared by the read and write paths, so an
    # entry can't read as still-capped but get pruned on write (or the
    # reverse) right at the edge of the retention window.
    if ts is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.CAPPED_RETENTION_DAYS)
    return ts >= cutoff


# The cap store is load-modify-save from web worker threads (each finished
# upgrade/downsample marks its album) and, in terminal mode, the CLI walks -
# so mutations hold both a thread lock and the cross-process store lock.
# Readers stay lockless; a snapshot is fine for them.
_capped_lock = threading.Lock()


def save_capped(data):
    # Prune stale entries before writing. is_album_capped treats them as "not
    # capped" but never removes them, so the file would otherwise accumulate
    # dead weight on every mark_album_capped call. Local policy entries are
    # durable because they record an intentional downsample decision.
    fresh = {
        k: v for k, v in (data or {}).items()
        if (v or {}).get("scope") == "local_album"
        or _capped_is_fresh(_capped_ts(v))
    }
    try:
        state_file.write_json(cfg.CAPPED_FILE, fresh, ensure_ascii=False)
    except OSError:
        return False
    return True


def is_album_capped(album_id, capped):
    if not album_id:
        return False
    return _capped_is_fresh(_capped_ts(capped.get(str(album_id))))


def _local_album_cap_key(album_dir):
    if not album_dir:
        return ""
    path = Path(album_dir)
    try:
        root = Path(cfg.MUSIC_ROOT).resolve(strict=False)
        resolved = path.resolve(strict=False)
        try:
            identity = str(resolved.relative_to(root))
        except ValueError:
            identity = str(resolved)
    except OSError:
        identity = str(path)
    digest = hashlib.sha256(identity.encode("utf-8", "surrogateescape")).hexdigest()
    return f"local:{digest}"


def is_local_album_capped(album_dir, capped, album=None):
    capped = capped or {}
    key = _local_album_cap_key(album_dir)
    if key and key in capped:
        return True
    # Path-independent fallback: a folder move / rename / migrate /
    # consolidate changes the path-derived key, but it's the same album the
    # user deliberately downsampled - so match the durable local cap by Qobuz
    # album id, then by a loose artist+title fingerprint (the same identity
    # the dismiss store uses). Without this, any reorg re-opens the
    # downsample→upgrade loop the cap exists to stop.
    if album:
        aid = str(album.get("id") or "")
        fp = hidden_mod.album_fingerprint(
            (album.get("artist") or {}).get("name") or "",
            album.get("title") or "")
        for v in capped.values():
            if not isinstance(v, dict) or v.get("scope") != "local_album":
                continue
            if aid and str(v.get("qobuz_album_id") or "") == aid:
                return True
            if fp and hidden_mod.album_fingerprint(
                    v.get("artist") or "", v.get("title") or "") == fp:
                return True
    return False


def clear_local_album_cap(album_dir):
    """Forget that an album was downsampled - the undo path.

    The cap keeps Upgrade from re-offering an album the user deliberately
    shrank. Restoring the hi-res originals clears the path key and any entry
    that still matches it by folder identity.
    """
    key = _local_album_cap_key(album_dir)
    if not key:
        return 0
    with _capped_lock, state_file.store_lock(cfg.CAPPED_FILE):
        return _clear_local_album_cap_locked(key, album_dir)


def _clear_local_album_cap_locked(key, album_dir):
    path = Path(album_dir)
    capped = load_capped()

    def _direct(k, v):
        return (k == key
                or (isinstance(v, dict) and v.get("scope") == "local_album"
                    and str(v.get("album_dir") or "") == str(path)))

    # is_local_album_capped also matches path-independently, by Qobuz id and
    # by artist+title fingerprint - so the undo has to clear by the same
    # identities, or an entry re-marked under a moved folder keeps
    # suppressing the album after the originals come back.
    ids, fps = set(), set()
    for k, v in capped.items():
        if not (_direct(k, v) and isinstance(v, dict)):
            continue
        if v.get("qobuz_album_id"):
            ids.add(str(v["qobuz_album_id"]))
        fp = hidden_mod.album_fingerprint(
            v.get("artist") or "", v.get("title") or "")
        if fp:
            fps.add(fp)
    drop = [k for k, v in capped.items()
            if _direct(k, v)
            or (isinstance(v, dict) and v.get("scope") == "local_album"
                and (str(v.get("qobuz_album_id") or "") in ids
                     or hidden_mod.album_fingerprint(
                         v.get("artist") or "", v.get("title") or "") in fps))]
    for k in drop:
        capped.pop(k, None)
    if drop:
        save_capped(capped)
    return len(drop)


def mark_album_capped(album_id, qobuz_album, post_qual):
    if not album_id:
        return False
    with _capped_lock, state_file.store_lock(cfg.CAPPED_FILE):
        return _mark_album_capped_locked(album_id, qobuz_album, post_qual)


def _mark_album_capped_locked(album_id, qobuz_album, post_qual):
    capped = load_capped()
    capped[str(album_id)] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "title": qobuz_album.get("title"),
        "artist": (qobuz_album.get("artist") or {}).get("name"),
        "qobuz_advertised": album_quality_label(qobuz_album),
        "actual_n_at_target": post_qual.get("n_at", 0),
        "actual_n_below": post_qual.get("n_below", 0),
        "actual_n_above": post_qual.get("n_above", 0),
    }
    return save_capped(capped)


def mark_local_album_capped(album_dir, qobuz_album=None, post_qual=None):
    key = _local_album_cap_key(album_dir)
    if not key:
        return False
    with _capped_lock, state_file.store_lock(cfg.CAPPED_FILE):
        return _mark_local_album_capped_locked(
            key, album_dir, qobuz_album, post_qual)


def _mark_local_album_capped_locked(key, album_dir, qobuz_album, post_qual):
    album_path = Path(album_dir)
    post_qual = post_qual or {}
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "scope": "local_album",
        "album_dir": str(album_path),
        "title": album_path.name,
        "artist": album_path.parent.name,
        "actual_n_at_target": post_qual.get("n_at", 0),
        "actual_n_below": post_qual.get("n_below", 0),
        "actual_n_above": post_qual.get("n_above", 0),
    }
    if qobuz_album:
        entry.update({
            "qobuz_album_id": qobuz_album.get("id"),
            "title": qobuz_album.get("title") or entry["title"],
            "artist": (qobuz_album.get("artist") or {}).get("name") or entry["artist"],
            "qobuz_advertised": album_quality_label(qobuz_album),
        })
    capped = load_capped()
    capped[key] = entry
    return save_capped(capped)


# ── Upgrade-candidate scan ────────────────────────────────────────────────────

def scan_artist_for_upgrades(artist_name, artist_dir, token, args, capped=None):
    """Silently scan all album dirs under artist_dir for quality upgrade candidates.

    Uses the same catalog pre-fetch + local matching strategy as gap-fill to
    minimise API round-trips. Falls back to per-folder search on catalog miss.

    Returns a list of dicts (one per upgradeable album):
      {qobuz_album, album_dir, n_below,
       existing_quality_label, target_quality_label}

    Only includes albums where:
      • Qobuz offers strictly higher quality (all_lower / mixed_below)
      • Existing tracks are present on disk
      • No bonus tracks would be lost (extras block wipe-and-replace)
      • --no-upgrade is not set
    """
    from qobuz_librarian.library.catalog import find_expanded_edition, find_qobuz_album_for_dir

    # Invalidate only THIS artist's cached subdir listing, not the whole cache.
    # This runs once per artist inside the bulk-upgrade loop; clear_scan_caches()
    # would wipe every other artist's warm listing (and force a flac_cache disk
    # flush) on each iteration, cold-rebuilding the entire library per artist.
    # This function only reads quality metadata, so the flush isn't needed here.
    drop_artist_subdirs_cache(artist_dir)
    album_dirs = list_artist_album_dirs(artist_dir)
    if not album_dirs:
        return []

    # Pre-fetch artist catalog once so per-folder matching stays local.
    # Failure here is non-fatal; find_qobuz_album_for_dir falls back to search.
    catalog = []
    try:
        artists = search_artists(artist_name, token, limit=cfg.ARTIST_LOOKUP_LIMIT)
        if artists:
            best_a = max(artists, key=lambda a: similarity(a.get("name", ""), artist_name))
            if similarity(best_a.get("name", ""), artist_name) >= cfg.ARTIST_NAME_THRESH:
                artist_id = best_a.get("id")
                catalog, _ = get_artist_albums(artist_id, token, limit=cfg.ARTIST_CATALOG_LIMIT)
    except QobuzError:
        pass

    # Grabbed singles sit out the upgrade walk unless the user opted in.
    single_store = None if cfg.UPGRADE_SINGLES_ENABLED else hidden_mod.load()
    candidates = []
    for album_dir in album_dirs:
        try:
            qobuz_album = find_qobuz_album_for_dir(
                album_dir, artist_name, token,
                prefer_hires=args.prefer_hires,
                catalog=catalog,
                target_dir=album_dir,
            )
        except AuthLost:
            raise
        except QobuzError:
            vlog(f"    QobuzError matching {album_dir.name}; skipping")
            continue

        if qobuz_album is None or not is_lossless_album(qobuz_album):
            continue

        # Skip albums Qobuz can't actually deliver at target.
        if capped and is_local_album_capped(album_dir, capped, qobuz_album):
            vlog(f"    {album_dir.name}: capped (locally downsampled)")
            continue
        if capped and is_album_capped(qobuz_album.get("id"), capped):
            vlog(f"    {album_dir.name}: capped (partial hi-res previously)")
            continue

        qobuz_tracks = (qobuz_album.get("tracks") or {}).get("items") or []
        if not qobuz_tracks:
            continue

        existing, _ = find_existing_tracks(qobuz_album)
        if not existing:
            continue  # nothing on disk - not an upgrade scenario

        # False-match guard: if no tracks overlap the Qobuz result is wrong.
        missing, present = compute_missing(qobuz_tracks, existing)
        if not present:
            continue

        # Key the single check on the matched Qobuz artist name - that's what
        # mark_single stored. The folder name (artist_name) can differ from it
        # ("Beatles" on disk vs "The Beatles" on Qobuz), which would miss the
        # mark and leak the downloaded single into the upgrade scan.
        if single_store is not None and missing:
            q_artist = (qobuz_album.get("artist") or {}).get("name") or artist_name
            if hidden_mod.is_single(
                q_artist,
                qobuz_album.get("title"),
                single_store,
                year=album_year(qobuz_album),
                album_id=qobuz_album.get("id"),
            ):
                continue

        if getattr(args, "no_upgrade", False):
            continue

        qual = compare_album_quality(existing, qobuz_album)
        if qual["classification"] not in ("all_lower", "mixed_below"):
            continue

        # An unreadable track can't be verified as an upgrade, and the
        # wipe-replace in process_album refuses the whole album when any
        # exist. Don't surface a candidate it would only no-op on.
        if qual["n_unknown"]:
            continue

        # Bonus tracks normally block wipe-and-replace. Before giving up, try
        # to find an alternate Qobuz edition that covers everything on disk.
        extras = find_extras_in_existing(qobuz_tracks, existing)
        if extras:
            cands = find_expanded_edition(qobuz_album, album_dir,
                                          existing, token, args)
            swapped = False
            for full, new_extras in cands:
                if new_extras:
                    continue
                # A swapped-to edition has its own id, so its cap marker is
                # separate from the one we checked above - honour it here or
                # a capped expanded edition re-flags on every scan.
                if capped and is_album_capped(full.get("id"), capped):
                    continue
                new_qual = compare_album_quality(existing, full)
                if new_qual["classification"] in ("all_lower", "mixed_below"):
                    qobuz_album = full
                    qobuz_tracks = (full.get("tracks") or {}).get("items") or []
                    qual = new_qual
                    swapped = True
                    break
            if not swapped:
                continue

        # Build human-readable quality labels for the upgrade summary.
        qbits, qrate = qual["qobuz_quality"]
        target_label = (f"{qbits}-bit/{qrate / 1000:g}kHz"
                        if qbits and qrate else "Qobuz quality")

        _qcounts: dict = {}
        for t in existing:
            bb, rr = existing_track_quality(t)
            if bb and rr:
                _qcounts[(bb, rr)] = _qcounts.get((bb, rr), 0) + 1
        if len(_qcounts) > 1:
            # Mixed qualities on disk. A majority label can EQUAL the target
            # (one stray CD track in a hi-res album), which reads as a no-op
            # upgrade - name the low end instead, since that's what the
            # upgrade fixes.
            lo_b, lo_r = min(_qcounts)
            existing_label = f"mixed, down to {lo_b}-bit/{lo_r / 1000:g}kHz"
        elif _qcounts:
            eb, er = next(iter(_qcounts))
            existing_label = f"{eb}-bit/{er / 1000:g}kHz"
        else:
            existing_label = "lower quality"

        # Capture confidence signals for --auto-safe.
        # 'extras' here is the var from the bonus-track guard above -
        # truthy means an edition swap was used to cover on-disk tracks.
        _folder_bare = strip_album_decorations(album_dir.name)
        _title_bare  = strip_album_decorations(qobuz_album.get("title", "") or "")
        _title_sim   = similarity(_folder_bare, _title_bare)
        _needed_swap = bool(extras)

        candidates.append({
            "qobuz_album":            qobuz_album,
            "album_dir":              album_dir,
            "n_present":              len(existing),
            "n_total":                len(qobuz_tracks),
            "n_below":                qual["n_below"],
            "existing_quality_label": existing_label,
            "target_quality_label":   target_label,
            "_needed_edition_swap":   _needed_swap,
            "_title_similarity":      _title_sim,
        })
        time.sleep(cfg.ARTIST_API_DELAY)

    return candidates
