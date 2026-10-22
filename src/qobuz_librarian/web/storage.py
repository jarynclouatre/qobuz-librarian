"""The mounted folders: host paths, writability, the music folder and the quality census."""
import os
import shutil
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian import disk_support
from qobuz_librarian.library import collection_snapshot, flac_cache
from qobuz_librarian.ui_cli.colors import format_size

# Docker makes a missing bind-mount source as an empty root-owned folder, so a
# mistyped path in .env looks like a permissions problem.
DOCKER_MADE = ("is an empty folder only root can write to, the kind Docker "
               "makes when the host folder named in .env does not exist")


def _docker_made_folder(path: Path) -> bool:
    try:
        return (cfg.in_container() and os.geteuid() != 0
                and path.stat().st_uid == 0 and not os.access(path, os.W_OK)
                and not any(path.iterdir()))
    except OSError:
        return False


def music_mounted_read_only() -> bool:
    """Whether the music folder is a read-only mount (`:ro`)."""
    try:
        return bool(os.statvfs(cfg.MUSIC_ROOT).f_flag & os.ST_RDONLY)
    except OSError:
        return False


def _unwritable_volumes() -> list[str]:
    """Live probe of the critical mounts; empty means writes may run.

    Probed on every gated attempt, so fixing ownership on the host opens
    the gate without a container restart; the Diagnostics page re-checks
    live, and the gate has to agree with it. Opt-in via env so tests and
    dev runs without /staging or /music mounted don't trip on it; the
    bundled compose sets it to 1."""
    raw_check_volumes = os.environ.get("QL_CHECK_VOLUMES")
    if raw_check_volumes is None:
        return []
    if not cfg._env_bool("QL_CHECK_VOLUMES", True):
        return []
    problems = []
    # A music folder mounted :ro is a choice, not a fault: scans run and
    # downloads are off, the way the read-only notice says.
    music_ro = music_mounted_read_only()
    # Named the way the operator would recognise the folder, not the compose
    # env var; the path shown is the host one when the compose file names it,
    # since a container path means nothing on the host that needs fixing.
    for friendly, path in (("Staging area", cfg.STAGING_DIR),
                           ("Music library", cfg.MUSIC_ROOT)):
        if friendly == "Music library" and music_ro:
            continue
        p = Path(path)
        unreachable = not p.exists()
        not_a_dir = p.exists() and not p.is_dir()
        unwritable = p.exists() and p.is_dir() and not os.access(str(p), os.W_OK)
        if unreachable or not_a_dir or unwritable:
            display, _ = _resolve_host_path(str(path))
            problems.append(
                f"{friendly} ({display})"
                + (" is missing" if unreachable
                   else " is not a folder" if not_a_dir
                   else f" {DOCKER_MADE}" if _docker_made_folder(p)
                   else " is read-only"))
    return problems


def _data_dir_available() -> bool:
    path = Path(cfg.DATA_DIR)
    try:
        return path.is_dir() and os.access(
            path, os.R_OK | os.W_OK | os.X_OK)
    except OSError:
        return False


def _music_root_hint() -> str:
    """Where the path in a music-folder message actually comes from. Inside the
    image it is the mount point, not anything the user typed, so naming the path
    alone sends them hunting for a folder their machine does not have."""
    if cfg.in_container():
        return ("That is the path inside the container: check which folder "
                "QL_MUSIC_DIR maps onto it in your .env.")
    return "Set MUSIC_ROOT to the folder that holds your artist folders."


def _music_write_target_message(state: str, recorded_albums: int = 0, *,
                                job_started: bool = False,
                                diagnostic: bool = False) -> str:
    root = Path(cfg.MUSIC_ROOT)
    hint = _music_root_hint()
    ending = "" if diagnostic else (
        "The download stopped before writing any files."
        if job_started else "Nothing was queued."
    )

    def finish(message):
        return f"{message} {ending}".rstrip()

    if state == "missing":
        return finish(f"{root} does not exist. {hint}")
    if state == "not_folder":
        return finish(f"{root} is not a folder. {hint}")
    if state == "unreadable":
        return finish(f"{root} could not be read. {hint}")
    if state == "backup_unreadable":
        return finish(
            f"No artist folders were found in {root}, and the last collection "
            "backup could not be read safely. Check that the music folder is "
            "mounted and that the collection-backup folder is readable."
        )
    if state == "recorded_empty":
        return finish(
            f"No artist folders were found in {root}, but the last collection "
            f"backup recorded {recorded_albums:,} "
            f"{'album' if recorded_albums == 1 else 'albums'}. Check that the "
            "music folder is mounted. If those albums really are gone, open "
            "Settings → Collection backup, choose Back up now, then Replace "
            "anyway before retrying."
        )
    return ""


# When a download stopped on a full music disk, and the music folder's free
# space then. Downloads queued before it fail at once until space is freed.
_music_disk_full: tuple[float, int] | None = None


def _music_free_bytes() -> int:
    try:
        return shutil.disk_usage(cfg.MUSIC_ROOT).free
    except OSError:
        return 0


def note_music_disk_full() -> None:
    global _music_disk_full
    _music_disk_full = (time.time(), _music_free_bytes())


def _refuse_behind_full_music_disk(job) -> None:
    global _music_disk_full
    if _music_disk_full is None:
        return
    stopped_at, free_then = _music_disk_full
    if _music_free_bytes() > free_then:
        _music_disk_full = None
    elif job.created_at < stopped_at:
        raise RuntimeError(
            "Not started: the disk filled up during an earlier download. "
            "Free some space, then retry.")


def _require_music_write_target_for_job() -> None:
    state, recorded_albums = collection_snapshot.music_root_write_state()
    if state != "ready":
        raise RuntimeError(_music_write_target_message(
            state, recorded_albums, job_started=True))
    refusal = disk_support.refusal("downloads")
    if refusal:
        raise RuntimeError(refusal)


_census_cache: tuple | None = None


_CENSUS_TTL = 300.0


def _is_mount_point(path) -> bool:
    """Whether ``path`` is the root of its own filesystem.

    Decides whether the free-space figure beside it covers the music alone or a
    disk shared with everything else on the machine, so the label can say which.
    """
    try:
        p = Path(path)
        return p.stat().st_dev != p.parent.stat().st_dev
    except OSError:
        return False


def _census_view():
    """Quality-census context for the Library page, shaped from the scan
    cache. One table walk over every cached tag row: cheap, but not
    per-request cheap on a big library, so the shaped result is memoized for
    a few minutes. None hides the panel (cache off, or nothing scanned yet)."""
    global _census_cache
    now = time.time()
    # A download or a downsample writes to the cache the moment it finishes,
    # here or in a terminal run, so the age of the memo is not enough on its
    # own: hold it only while the rows behind it have not changed.
    stamp = flac_cache.store_stamp()
    if (_census_cache is not None
            and _census_cache[2] == stamp
            and now - _census_cache[0] < _CENSUS_TTL):
        return _census_cache[1]
    raw = flac_cache.census()
    view = None
    if raw:
        labels = {
            "cd": "CD quality (16-bit / 44.1–48 kHz)",
            "hires48": "Hi-res up to 48 kHz",
            "hires96": "Hi-res up to 96 kHz",
            "hires192": "Hi-res above 96 kHz",
            "unknown": "Other formats",
        }
        seg = {"cd": "cd", "hires48": "h48", "hires96": "h96",
               "hires192": "h192", "unknown": "other"}
        total_bytes = raw["total_bytes"] or 1
        rows, bar = [], []
        for tier in ("cd", "hires48", "hires96", "hires192", "unknown"):
            n, size = raw["tiers"][tier]
            if not n:
                continue
            rows.append({"key": seg[tier], "label": labels[tier],
                         "tracks": f"{n:,} track{'s' if n != 1 else ''}",
                         "size": format_size(size)})
            bar.append({"key": seg[tier],
                        "pct": max(1, round(100 * size / total_bytes))})
        view = {
            "total": f"{raw['total_tracks']:,} tracks · "
                     f"{format_size(raw['total_bytes'])}",
            "rows": rows,
            "bar": bar,
            "top": [{"name": a, "size": format_size(b)}
                    for a, b in raw["top_hires_artists"]],
            # Below ~100 MB the line is noise, not an offer.
            "reclaim": (format_size(raw["reclaim_bytes"])
                        if raw["reclaim_bytes"] >= 100 * 1024 * 1024 else ""),
        }
    # Re-read: census() drains any buffered writes first, so the count it was
    # actually built from is the one after that flush.
    _census_cache = (now, view, flac_cache.store_stamp())
    return view


# The bundled compose file passes each bind mount's host folder in under the
# .env name that sets it.
_HOST_FOLDERS = (
    ("/music", "QL_MUSIC_DIR"),
    ("/staging", "QL_STAGING_DIR"),
    ("/upgrade_backups", "QL_UPGRADE_BACKUPS"),
    ("/collection_backups", "QL_COLLECTION_BACKUPS"),
)


def _resolve_host_path(container_path: str) -> tuple[str, bool]:
    """Return (display_path, is_host_path) for a path inside the container.

    Only the compose file knows which host folder a mount comes from, so a
    path is translated only under a mount whose host folder it passed in.
    Anything else, and every path outside a container, is shown as it is.
    """
    container_path = str(container_path)
    if not cfg.in_container():
        return container_path, False
    for mount_point, variable in _HOST_FOLDERS:
        host_root = os.environ.get(variable, "").strip()
        if not host_root:
            continue
        if container_path == mount_point:
            return host_root, True
        if container_path.startswith(mount_point + "/"):
            suffix = container_path[len(mount_point):]
            return host_root.rstrip("/") + suffix, True
    return container_path, False
