"""Streamrip download wrapper and staging utilities."""
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations.staging import (
    StagedFile,
    StagingSnapshot,
    capture_file,
    inspect_retry_groups,
    list_file_groups,
    list_groups,
    quarantine_file,
    reconcile_legacy_file_groups,
)
from qobuz_librarian.recovery import normalise_recovery_owner
from qobuz_librarian.ui_cli.colors import C, fmt
from qobuz_librarian.ui_cli.logging import log, vlog, wrap_thread_target

# Optional cancel-check hook. The web JobManager installs a callable here that
# returns True when the active job has been cancelled; rip_url polls it
# between proc.wait iterations and kills the subprocess group when it fires.
_CANCEL_CHECK = None


def set_cancel_check(fn):
    """Register a no-arg callable that returns True to cancel the active rip.

    Called by qobuz_librarian.web.jobs at import time.
    """
    global _CANCEL_CHECK
    _CANCEL_CHECK = fn


def is_cancel_requested():
    """True when the active web job has been cancelled. Always False on the
    CLI path (no hook installed; Ctrl-C raises KeyboardInterrupt instead).
    Lets post-rip code skip the beets import after a cancel."""
    return bool(_CANCEL_CHECK and _CANCEL_CHECK())

try:
    from mutagen.flac import FLAC as MutagenFLAC
    HAVE_MUTAGEN = True
except Exception:
    MutagenFLAC = None  # type: ignore
    HAVE_MUTAGEN = False

_FLAC_TRUNCATION_FLOOR = 150_000


# ── FLAC signature ────────────────────────────────────────────────────────────

def _flac_signature(path: Path):
    """Tag-tuple identifier for a FLAC, stable across beets's move. Lyric_fetch's
    state file is keyed by absolute path.
    """
    if not HAVE_MUTAGEN:
        return None
    try:
        f = MutagenFLAC(str(path))
    except Exception:
        return None
    if f.tags is None:
        return None

    def _g(*keys):
        for k in keys:
            v = f.tags.get(k)
            if v and isinstance(v, list) and v[0]:
                return str(v[0]).strip()
        return ""

    from qobuz_librarian.library.scanner import parse_track_num
    try:
        disc = int(parse_track_num(_g("discnumber", "DISCNUMBER")) or 1) or 1
    except (TypeError, ValueError):
        disc = 1
    try:
        track = int(parse_track_num(_g("tracknumber", "TRACKNUMBER")) or 0)
    except (TypeError, ValueError):
        track = 0

    return (
        _g("albumartist", "ALBUMARTIST", "artist", "ARTIST").lower(),
        _g("album", "ALBUM").lower(),
        disc,
        track,
        _g("title", "TITLE").lower(),
    )


# ── FLAC validation ───────────────────────────────────────────────────────────

def flac_audio_ok(path, *, descriptor=None):
    """Verify a FLAC's audio with ``flac -t`` (decode + per-frame CRC check).

    Returns True/False, or None when the flac tool isn't installed so the
    caller can choose its own fallback. ``flac -t`` reads only the audio
    stream — the embedded cover art streamrip always writes (sometimes the
    oversized "original" Qobuz art) never enters the decode, so a track with
    intact audio but a malformed picture isn't mistaken for a broken download.
    A verify that exceeds the timeout reads as broken (False), not unverifiable
    — FLAC checks far faster than real time, so a hang means a pathological file,
    and None is reserved for the tool being absent."""
    if shutil.which("flac") is None:
        return None
    try:
        command_path = str(path)
        run_kwargs = {}
        if descriptor is not None:
            command_path = f"/proc/self/fd/{int(descriptor)}"
            run_kwargs["pass_fds"] = (int(descriptor),)
        proc = subprocess.run(
            ["flac", "-t", "-s", command_path],
            capture_output=True, timeout=300, stdin=subprocess.DEVNULL,
            **run_kwargs)
    except subprocess.TimeoutExpired:
        return False
    except (FileNotFoundError, OSError):
        return None
    return proc.returncode == 0


def flac_audio_offset(path, *, descriptor=None):
    """Byte offset of the first audio frame — the ``fLaC`` marker plus every
    metadata block. Lets a caller weigh the audio stream apart from metadata and
    embedded art, which don't shrink on resample and survive the tail damage that
    truncates the audio. Returns 0 when the file isn't a plain FLAC or the header
    is unreadable, so callers fall back to a whole-file size, not a wrong answer."""
    try:
        if descriptor is None:
            fh = open(path, "rb")
        else:
            fh = open(f"/proc/self/fd/{int(descriptor)}", "rb")
        with fh:
            if fh.read(4) != b"fLaC":
                return 0
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(4)
            offset = 4
            while True:
                header = fh.read(4)
                if len(header) < 4:
                    return 0
                is_last = bool(header[0] & 0x80)
                offset += 4 + int.from_bytes(header[1:4], "big")
                # A corrupt/garbage block length can push the offset past EOF;
                # don't trust it — it would collapse a caller's audio_size to 0
                # and false-flag a healthy file as truncated. Fall back to the
                # whole-file size instead.
                if offset > size:
                    return 0
                fh.seek(offset)
                if is_last:
                    return offset
    except OSError:
        return 0


def is_flac(path: Path) -> bool:
    """True when the file is a complete, decodable FLAC.

    An interrupted streamrip download leaves a truncated file behind whose
    header still advertises the full duration, so a metadata probe reports
    the whole track length even though most audio frames are missing — only
    verifying the frames exposes the gap. Without catching it, partials
    inflate n_ok, beets silently skips them on import, and the upgrade flow
    declares a false success.

    ``flac -t`` does that verification (every frame's CRC). When the flac tool
    isn't installed we fall back to a size heuristic: a few seconds of FLAC
    sits well above the floor, so anything smaller is almost certainly a
    partial; larger files we can't verify, so we trust them."""
    try:
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False

    ok = flac_audio_ok(path)
    if ok is not None:
        if not ok:
            log.info(fmt(C.YELLOW,
                f"  ⚠  {path.name} won't decode cleanly (truncated/corrupt); "
                "treating as broken."))
        return ok

    try:
        small = path.stat().st_size < _FLAC_TRUNCATION_FLOOR
    except OSError:
        small = True
    if small:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {path.name} suspiciously small and flac tool unavailable; "
            "treating as broken."))
        return False
    log.info(fmt(C.YELLOW,
        f"  ⚠  flac tool unavailable for {path.name}; trusting .flac extension."))
    return True


# ── Process helpers ───────────────────────────────────────────────────────────

def _kill_process_group(proc):
    """Best-effort: kill rip's whole process group, fall back to .kill().

    Guard against the start_new_session race: between Popen returning and
    the child running setsid(), the child's pgid is still *ours*. A
    killpg(SIGKILL) then would SIGKILL this process and the user's shell.
    Only group-kill once the child is verifiably in its own group;
    otherwise kill just the child pid.
    """
    try:
        pgid = os.getpgid(proc.pid)
        if pgid != os.getpgrp():
            os.killpg(pgid, signal.SIGKILL)
            return
    except OSError:
        pass  # child already gone, or getpgid failed → fall through
    try:
        proc.kill()  # child pid only — never our own group
    except OSError:
        pass


def _terminate(proc, reader):
    """Kill the rip process group, reap the child, and drain the reader.

    Reaping matters: in the container uvicorn runs as PID 1 with no init to
    harvest orphans, so a killed rip left unwaited stays a zombie for the
    life of the web process, one per cancel or timeout. Once the child is
    reaped its stdout is closed, so joining the reader afterwards can't race
    the ``lines`` buffer the caller is about to read.
    """
    _kill_process_group(proc)
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    reader.join(timeout=5)


# ── streamrip wrapper ─────────────────────────────────────────────────────────

def rip_url(url, timeout=None, live_output=False, quality=None,
            staging_dir=None):
    """Run `rip url <url>`, returning (returncode, combined_output_string).

    live_output=True: stream rip's output to the terminal in real time via a
    reader thread — used for full-album downloads where silence for many minutes
    is unacceptable on mobile.

    start_new_session=True puts rip in its own process group so that on timeout
    OR Ctrl-C, os.killpg kills the entire tree (rip spawns its own downloader
    children; plain process.kill() only kills the parent, leaving them orphaned).
    """
    if timeout is None:
        timeout = cfg.RIP_TIMEOUT
    if quality is None:
        quality = cfg.STREAMRIP_QUALITY
    cmd = ["rip"]
    # The librarian does its own missing-track bookkeeping (it re-rips for
    # quality upgrades, truncation repair, and broken-FLAC retries), so
    # streamrip's downloads database — which silently skips any URL it has
    # already logged, exiting 0 with no new file — must be off. The Docker
    # entrypoint forces downloads_enabled=false in the config, but a
    # bare-metal/CLI run can fall back to the user's own
    # ~/.config/streamrip/config.toml where it defaults ON; --no-db (a global
    # rip flag) makes every re-rip work regardless of the config.
    cmd += ["--no-db"]
    # streamrip does NOT honor a STREAMRIP_CONFIG env var; it looks at
    # ~/.config/streamrip/config.toml unless --config-path is passed. The
    # web Settings page writes credentials to cfg.STREAMRIP_CONFIG, so we
    # must point rip at that file explicitly or the saved creds are ignored.
    cmd += ["--config-path", str(cfg.STREAMRIP_CONFIG)]
    if staging_dir is not None:
        cmd += ["--folder", str(staging_dir)]
    if quality is not None:
        cmd += ["-q", str(quality)]
    cmd += ["url", url]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "rip: command not found"

    lines = []

    # Pre-compiled filters to strip Rich rules, timestamps, and
    # collapse each multi-line ERROR block to one concise line.
    _ts_re   = re.compile(r"^\s*\[\d{2}:\d{2}:\d{2}\]\s*")
    _rule_re = re.compile(r"^\s*[─━]{3,}")
    # Any line starting with a Unicode box-drawing char (U+2500–U+257F) is
    # part of a Rich panel/traceback. Streamrip prints a multi-line traceback
    # box on its `version_coro` post-processing bug; this filters the noise
    # out of live output.
    _box_re  = re.compile(r"^[\u2500-\u257f]")
    # Rich's log handler appends "  module.py:LINE" columns at end of line —
    # anchor to EOL so a track title containing the literal "name.py:42"
    # isn't mangled.
    _src_re  = re.compile(r"\s+\w+\.py:\d+\s*$")
    # Rich also prefixes records with the log level name ("INFO     …").
    # Drop DEBUG/INFO/WARNING/CRITICAL but leave ERROR intact so the
    # existing multi-line error-grouping logic can detect it. \s* tolerates
    # Rich's left-padding on narrow terminals.
    _lvl_re  = re.compile(r"^\s*(?:DEBUG|INFO|WARNING|CRITICAL)\s+")

    def _reader():
        banner_lines_remaining = 0
        in_error = False
        err_buf = []

        def emit_error():
            nonlocal in_error, err_buf
            if err_buf:
                text = " ".join(s.strip() for s in err_buf if s.strip())
                text = re.sub(r"\s+", " ", text)
                m = re.search(r"Persistent error downloading track '([^']+)', skipping", text)
                if m:
                    log.info(fmt(C.RED, f"    ✗ skipped: {m.group(1)} (network)"))
                else:
                    m = re.search(r"Error downloading track '([^']+)', retrying", text)
                    if m:
                        log.info(fmt(C.YELLOW, f"    ⟳ retry: {m.group(1)}"))
                    else:
                        short = text[:100] + ("…" if len(text) > 100 else "")
                        log.info(fmt(C.RED, "    ✗ " + short))
            in_error = False
            err_buf = []

        for line in proc.stdout:
            lines.append(line)
            if not live_output:
                continue
            stripped = line.strip()
            if banner_lines_remaining == 0 and (
                "new version of streamrip" in stripped.lower()
                or "pip install streamrip" in stripped.lower()
                or "pip3 install streamrip" in stripped.lower()
            ):
                banner_lines_remaining = 40
                continue
            if banner_lines_remaining > 0:
                banner_lines_remaining -= 1
                continue
            if not stripped or _rule_re.match(stripped) or _box_re.match(stripped):
                if in_error: emit_error()
                continue
            cleaned = _ts_re.sub("", line.rstrip())
            if "ERROR" in cleaned[:40]:
                if in_error: emit_error()
                in_error = True
                err_buf = [cleaned]
                continue
            if in_error:
                if line.startswith("    "):
                    err_buf.append(cleaned)
                    continue
                emit_error()
            log.info("    " + _src_re.sub("", _lvl_re.sub("", cleaned)))
        emit_error()

    # Wrap so the reader thread inherits the spawning job's context — its
    # streamrip progress + per-track error lines log via the shared logger and
    # would otherwise be dropped by the job-log handler's thread filter.
    reader = threading.Thread(target=wrap_thread_target(_reader), daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout if timeout else None
    try:
        while True:
            if _CANCEL_CHECK is not None and _CANCEL_CHECK():
                _terminate(proc, reader)
                return 130, "".join(lines) + "\n<<< canceled by user >>>"
            poll_for = 1.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                poll_for = min(poll_for, remaining)
            try:
                proc.wait(timeout=poll_for)
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        _terminate(proc, reader)
        # 124 (the conventional timeout exit code) so a timeout is
        # distinguishable from a generic rip failure (rc=1) in the logs.
        return 124, "".join(lines) + (
            f"\n<<< rip timed out after {timeout}s — try a smaller batch "
            "or raise RIP_TIMEOUT in compose.yaml. >>>")
    except KeyboardInterrupt:
        # Ctrl-C: with start_new_session=True the SIGINT did NOT propagate to
        # rip's group, so rip + its children would keep running after this
        # process exits — wasted bandwidth, half-written staging files.
        _terminate(proc, reader)
        raise

    reader.join(timeout=5)
    return proc.returncode, "".join(lines)


# ── Staging snapshot / cleanup ────────────────────────────────────────────────

def _iter_staging_files():
    """Yield every staging file, skipping the retry-park tree entirely.

    .beets_retry/ holds re-import-queued album dirs that accumulate over a
    long-running session; walking them on every snapshot was the dominant
    per-album cost on big queue flushes, when nothing about those files
    matters here. Skipping the tree (rather than rglob-then-filter) keeps
    the walk proportional to real staging activity, not the parked backlog.
    """
    if not cfg.STAGING_DIR.exists():
        return
    retry_name = cfg.BEETS_RETRY_DIR
    for entry in cfg.STAGING_DIR.iterdir():
        if entry.name == retry_name:
            continue
        if entry.is_file():
            yield entry
        elif entry.is_dir():
            yield from (p for p in entry.rglob("*") if p.is_file())


def snapshot_staging():
    sealed = []
    for path in _iter_staging_files():
        receipt = capture_file(path)
        if receipt is not None:
            sealed.append(receipt)
    return StagingSnapshot(sealed)


def files_added_since(prior_snapshot):
    added = []
    identities = getattr(prior_snapshot, "identities", {})
    for path in _iter_staging_files():
        if path in prior_snapshot:
            # A pre-existing name whose identity changed is not output owned by
            # this run. Leave it alone instead of adopting the replacement.
            if identities:
                receipt = capture_file(path)
                if (receipt is not None
                        and receipt.identity != identities.get(path)):
                    continue
            continue
        receipt = capture_file(path)
        if receipt is not None:
            added.append(receipt)
    return added


def cleanup_lossy(new_files, *, owner=None, on_intent=None):
    """Sort freshly-downloaded audio into (kept, lossy, broken), setting both
    kinds of reject aside. Returns Paths for all three buckets so the
    caller can retain enough identity to retry the exact rejected track:

      lossy   — a non-FLAC file (.mp3/.m4a/…). Qobuz served lossy because no
                lossless master is available for the user's tier; another
                source would be needed.
      broken  — a .flac that won't decode (truncated/interrupted download).
                A re-rip usually fixes it.
    """
    owner = normalise_recovery_owner(owner)
    if (owner is None) != (on_intent is None):
        raise ValueError("owned reject cleanup requires an intent checkpoint")
    if on_intent is not None and not callable(on_intent):
        raise ValueError("reject cleanup checkpoint must be callable")

    kept, lossy, broken = [], [], []
    for f in new_files:
        receipt = f if isinstance(f, StagedFile) else capture_file(f)
        path = Path(f)
        ext = f.suffix.lower()
        if ext == ".flac":
            if is_flac(path):
                # Validation belongs to the exact receipt, not the public
                # filename. A replacement that arrived while flac(1) ran is
                # neither credited nor moved by this run.
                if (receipt is not None
                        and capture_file(
                            receipt.path, expected=receipt.identity) is not None):
                    kept.append(receipt)
                else:
                    log.info(fmt(C.YELLOW,
                        f"  ⚠  {path.name} changed while it was being checked; "
                        "it was left untouched and will not be imported."))
                continue
            bucket, what = broken, "broken FLAC"
        elif ext in cfg.AUDIO_EXTS:
            bucket, what = lossy, "non-FLAC"
        else:
            continue
        # Record the reject whether or not we can delete it, so the caller's
        # counts and per-track retry see it. A reject left in staging gets
        # imported by beets (move: yes, autotag: no) — the exact lossy/truncated
        # file this discard exists to keep out — so when the unlink fails, move
        # it out of the import tree rather than leaving it to be picked up.
        bucket.append(receipt or f)
        if owner is None:
            quarantined = (
                receipt is not None and _quarantine_reject(receipt))
        else:
            quarantined = (
                receipt is not None
                and _quarantine_reject(
                    receipt, owner=owner, on_intent=on_intent)
            )
        if quarantined:
            vlog(f"set aside {what} {f.name} so it isn't imported")
        else:
            log.info(fmt(C.YELLOW,
                f"  ⚠  Couldn't safely set aside {what} {f.name}; it changed "
                "after the download and was left untouched."))
    return kept, lossy, broken


def _quarantine_reject(path, *, owner=None, on_intent=None):
    """Move an exact reject to private staging recovery so beets skips it."""
    if owner is None:
        result = quarantine_file(path, ".rejected")
    else:
        result = quarantine_file(
            path, ".rejected", owner=owner, on_intent=on_intent)
    if result is not None and not result.durable:
        vlog(
            f"set aside {Path(path).name}, but its recovery directory sync "
            "could not be confirmed"
        )
    return result is not None


def cleanup_staging_residue():
    """Keep legacy residue for preflight instead of deleting by name alone."""
    reconciled = reconcile_legacy_file_groups()
    if reconciled:
        vlog(f"recorded {reconciled} legacy rejected staging file(s) for recovery")
    unresolved = list_file_groups()
    if unresolved:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {len(unresolved)} staging file(s) are retained in "
            f"{cfg.BEETS_RETRY_DIR} for retry or manual recovery."))
    interrupted = list_groups(kind="interrupted")
    if interrupted:
        log.info(fmt(C.YELLOW,
            f"  ⚠  {len(interrupted)} interrupted download(s) are retained in "
            f"{cfg.BEETS_RETRY_DIR} for manual recovery."))
    inspections = inspect_retry_groups()
    hidden = [
        inspection for inspection in inspections
        if inspection.status in {"incomplete", "malformed", "blocked"}
    ]
    if hidden:
        counts = {
            status: sum(item.status == status for item in hidden)
            for status in ("incomplete", "malformed", "blocked")
        }
        summary = ", ".join(
            f"{count} {status}" for status, count in counts.items() if count)
        log.info(fmt(C.YELLOW,
            f"  ⚠  {len(hidden)} staging recovery group(s) need attention "
            f"({summary}) in {cfg.BEETS_RETRY_DIR}."))
    # Inspection is visibility, not cleanup. No residue is deleted here.
    return 0
