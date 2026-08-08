"""pytest shared fixtures and session-wide isolation.

Redirects DATA_DIR (and the file paths derived from it) into a temp dir
so a `pytest -q` run doesn't write a real lock file / lyric-state file /
fetch log under ``~/.local/share/qobuz-librarian/`` on the dev machine.

Individual tests still monkeypatch specific paths via ``tmp_path`` for
finer-grained control; this fixture covers the global side effects of
importing the package and exercising web routes in tests.
"""
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_data_dir():
    """Point cfg.DATA_DIR and the files derived from it at a temp dir for
    the test session, so tests don't pollute the dev machine's HOME.

    Also default WEB_AUTH=none for the session so the web tests exercise the
    routes directly; the auth-specific tests flip it back on with monkeypatch,
    which restores this default on teardown.
    """
    from qobuz_librarian import config as cfg

    # CLI output wraps to the terminal, so pin a width: otherwise tests that
    # assert on a message pass or fail depending on how wide the window running
    # pytest happens to be.
    os.environ["COLUMNS"] = "100"

    tmp_root = Path(tempfile.mkdtemp(prefix="qobuz-librarian-tests-"))
    cfg.DATA_DIR = tmp_root

    # Re-derive every path that was built off DATA_DIR at import time.
    cfg.FETCH_LOG_FILE       = tmp_root / ".qobuz_librarian_log.json"
    cfg.WALK_SEEN_FILE       = tmp_root / ".qobuz_walk_seen.txt"
    cfg.ALBUM_WALK_SEEN_FILE = tmp_root / ".qobuz_album_walk_seen.txt"
    cfg.PENDING_QUEUE_FILE   = tmp_root / ".qobuz_pending_queue.json"
    cfg.QUEUE_JOURNAL_DIR    = tmp_root / ".qobuz_queue_journals"
    cfg.LYRIC_RETRY_FILE     = tmp_root / ".qobuz_lyric_retry.json"
    cfg.REPAIR_LOG_PATH      = tmp_root / ".qobuz_replaced_tracks.log"
    cfg.CAPPED_FILE          = tmp_root / ".qobuz_upgrade_capped.json"
    cfg.LIBRARY_SCAN_STATE_FILE = tmp_root / ".qobuz_library_scan_state.json"
    cfg.HIDDEN_FILE          = tmp_root / ".qobuz_hidden.json"
    cfg.REVIEW_BADGE_STATE_FILE = tmp_root / ".qobuz_review_badges.json"
    cfg.SCAN_SEEN_FILE       = tmp_root / ".qobuz_scan_seen.json"
    cfg.NEW_RELEASE_STATE_FILE = tmp_root / ".qobuz_new_releases.json"
    cfg.SCAN_CHECKPOINT_FILE = tmp_root / ".qobuz_scan_checkpoint.json"
    cfg.LYRIC_FETCH_STATE_FILE = tmp_root / ".lyric_fetch_state.json"
    cfg.WEB_AUTH_FILE        = tmp_root / ".qobuz_web_auth.json"
    cfg.LOCK_FILE            = tmp_root / "qobuz_librarian.lock"
    # Isolate the Qobuz credential source too - load_qobuz_token reads from
    # STREAMRIP_CONFIG by default, and the dev's real ~/.config/streamrip/
    # config.toml (which may carry a live token after sync_streamrip_creds_from_env
    # ever ran) would otherwise make every "no creds → /settings" route test
    # silently follow the happy path instead.
    cfg.STREAMRIP_CONFIG     = tmp_root / "streamrip" / "config.toml"
    cfg.QOBUZ_USER_AUTH_TOKEN = ""
    cfg.QOBUZ_USER_ID = ""
    cfg.BEETS_PYTHON = ""
    cfg.BEETS_CONFIG_DIR = tmp_root / "beets"
    cfg.BEETS_CONFIG_DIR.mkdir()
    cfg.BEETS_DB_PATH = cfg.BEETS_CONFIG_DIR / "musiclibrary.db"
    # Keep MUSIC_ROOT off the dev's real ~/Music - tests that need a library
    # build one under tmp_path and monkeypatch it, but the session default must
    # never be a real path a stray scan could walk.
    cfg.MUSIC_ROOT = tmp_root / "music"
    cfg.MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
    # The dashboard auto-runs the new-release check (when due) and the first-run
    # library scan; both off for the suite so unrelated GET / tests don't fire a
    # real background scan. The dedicated tests flip them on with monkeypatch.
    cfg.NEW_RELEASE_CHECK_INTERVAL = 0
    cfg.AUTO_LIBRARY_SCAN = False
    # Keep the persistent caches out of the deterministic suite - tests that
    # mock qobuz_get / build fixture FLACs expect a fresh read each time.
    cfg.ALBUM_CACHE_ENABLED  = False
    cfg.FLAC_CACHE_ENABLED   = False
    cfg.REPAIR_CACHE_ENABLED = False
    # No inter-lookup pacing in the deterministic suite - the repair tests mock
    # the Qobuz lookup, so a real sleep between calls would only add dead time.
    cfg.REPAIR_LOOKUP_MIN_INTERVAL = 0.0
    # Suppress write-through job persistence for the deterministic suite - a
    # shared jobs.db would otherwise leak historical rows between tests.
    from qobuz_librarian.web import job_persistence
    job_persistence._disabled = True

    # Set the matching env vars too, so a test that importlib.reload(cfg) (to
    # exercise env parsing) recomputes these into the temp dir / off, rather than
    # reverting to the real HOME paths and a live auto-check for the rest of the
    # session.
    prior_env = {k: os.environ.get(k) for k in
                 ("COLUMNS",
                  "WEB_AUTH", "DATA_DIR", "NEW_RELEASE_CHECK_INTERVAL",
                  "AUTO_LIBRARY_SCAN", "ALBUM_CACHE_ENABLED", "FLAC_CACHE_ENABLED",
                  "REPAIR_CACHE_ENABLED",
                  "UPGRADE_SCAN_ENABLED",
                  "BEETS_PYTHON",
                  "BEETS_CONFIG_DIR", "BEETS_DB_PATH",
                  "QOBUZ_USER_AUTH_TOKEN", "QOBUZ_USER_ID", "STREAMRIP_CONFIG",
                  "MUSIC_ROOT")}
    os.environ["WEB_AUTH"] = "none"
    os.environ["DATA_DIR"] = str(tmp_root)
    os.environ["NEW_RELEASE_CHECK_INTERVAL"] = "0"
    os.environ["AUTO_LIBRARY_SCAN"] = "false"
    os.environ["UPGRADE_SCAN_ENABLED"] = "true"
    # The caches are env-backed too so a test that importlib.reload(cfg) keeps
    # them off (their derived paths recompute under the temp DATA_DIR) instead
    # of reverting on for the rest of the session.
    os.environ["ALBUM_CACHE_ENABLED"] = "false"
    os.environ["FLAC_CACHE_ENABLED"] = "false"
    os.environ["REPAIR_CACHE_ENABLED"] = "false"
    os.environ["BEETS_PYTHON"] = ""
    os.environ["BEETS_CONFIG_DIR"] = str(cfg.BEETS_CONFIG_DIR)
    os.environ["BEETS_DB_PATH"] = str(cfg.BEETS_DB_PATH)
    # Same reasoning: clear the Qobuz creds env so a reload(cfg) in a test
    # doesn't pick a leftover dev-shell token back up.
    os.environ["QOBUZ_USER_AUTH_TOKEN"] = ""
    os.environ["QOBUZ_USER_ID"] = ""
    os.environ["STREAMRIP_CONFIG"] = str(cfg.STREAMRIP_CONFIG)
    # Set MUSIC_ROOT in the env too so a test that importlib.reload(cfg) keeps it
    # in the temp dir instead of reverting to the real ~/Music.
    os.environ["MUSIC_ROOT"] = str(cfg.MUSIC_ROOT)

    yield

    for k, v in prior_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # Clean up the session tempdir, best-effort. ignore_errors=True keeps
    # the test exit clean if a background worker is still tearing down.
    import shutil
    shutil.rmtree(tmp_root, ignore_errors=True)


@pytest.fixture
def restore_config():
    """For tests that ``importlib.reload(cfg)`` under patched env vars: reload it
    once more after the test so the recomputed module globals don't leak into the
    rest of the session. Request it BEFORE ``monkeypatch`` in the test signature
    so its teardown runs LAST - after monkeypatch has restored the env - and the
    reload recomputes against the session's (temp-dir) values, not the test's."""
    yield
    import importlib

    from qobuz_librarian import config
    importlib.reload(config)


@pytest.fixture(autouse=True)
def _clean_job_registry():
    """Clear any jobs a test left in the shared registry singleton so they don't
    leak into the next test - the registry is module-level and shared across the
    whole session, so a job one test adds (and doesn't remove) otherwise shows up
    in another test's registry scans / _get_reviewable_job lookups."""
    yield
    from qobuz_librarian.web import jobs as job_mgr
    with job_mgr.registry._lock:
        job_mgr.registry._jobs.clear()
        job_mgr.registry._order.clear()


@pytest.fixture(autouse=True)
def _allow_in_memory_job_admission(monkeypatch):
    """Keep ordinary tests independent of jobs.db without weakening runtime."""
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(job_persistence, "admit", lambda _job: True)
    monkeypatch.setattr(
        job_persistence,
        "admit_review_transition",
        lambda _job, _parked=(): True,
    )


@pytest.fixture(autouse=True)
def _fast_qobuz_retries(monkeypatch):
    """The client retries transient failures (429/5xx/network errors) with
    exponential backoff. In tests that's pure dead time - patch the
    indirection to a no-op so the suite stays fast. (Don't patch
    time.sleep globally; other modules sleep too.)"""
    monkeypatch.setattr("qobuz_librarian.api.client._retry_sleep", lambda *_: None)
