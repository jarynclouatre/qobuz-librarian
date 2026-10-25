"""Fault-injection tests for the Web job durability boundary."""

import queue
import sqlite3

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.web import job_persistence
from qobuz_librarian.web import jobs as jm
from qobuz_librarian.web.jobs import Job, JobStatus

_REAL_ADMIT = job_persistence.admit
_REAL_ADMIT_REVIEW = job_persistence.admit_review_transition


class _FailCommitOnce:
    """Forward to SQLite but fail the first commit after its write began."""

    def __init__(self, connection):
        self._connection = connection
        self._fail_commit = True

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def execute(self, *args, **kwargs):
        return self._connection.execute(*args, **kwargs)

    def commit(self):
        if self._fail_commit:
            self._fail_commit = False
            raise sqlite3.OperationalError("injected commit failure")
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()

    def close(self):
        return self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_failed_job_commit_cannot_ride_a_later_successful_commit(
        monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    connection = _FailCommitOnce(job_persistence._conn)
    monkeypatch.setattr(job_persistence, "_conn", connection)

    rejected = Job(title="Rejected admission")
    assert job_persistence.ready_for_admission() is True
    assert _REAL_ADMIT(rejected) is False
    assert job_persistence.ready_for_admission() is False
    assert connection.in_transaction is False

    accepted = Job(title="Accepted admission")
    assert _REAL_ADMIT(accepted) is True
    assert job_persistence.ready_for_admission() is True

    observer = sqlite3.connect(cfg.DATA_DIR / "jobs.db")
    try:
        saved_ids = {
            row[0] for row in observer.execute("SELECT id FROM jobs").fetchall()
        }
    finally:
        observer.close()

    assert rejected.id not in saved_ids
    assert accepted.id in saved_ids


@pytest.mark.parametrize("kind", ["library", "new_releases"])
def test_cancel_approved_review_preserves_picks_after_reload(
        monkeypatch, tmp_path, kind):
    from qobuz_librarian.web import job_runs, routes_jobs

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    job_persistence.init()
    monkeypatch.setattr(job_persistence, "admit", _REAL_ADMIT)
    monkeypatch.setattr(job_persistence, "admit_review_transition", _REAL_ADMIT_REVIEW)
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    work = queue.Queue()
    monkeypatch.setattr(jm, "_scan_queue", work)

    def scan(job):
        job.add_candidate("album", "Picked", payload={"album_id": "picked"},
                          selected=False)
        job.add_candidate("album", "Left parked", payload={"album_id": "parked"},
                          selected=False)

    job = jm.Job(title="Library scan", execute_kind=kind)
    assert jm.submit_scan(job, scan, lambda *_: None) is job
    queued, run = work.get_nowait()
    run(queued)
    assert job.status == jm.JobStatus.AWAITING_REVIEW
    assert job.set_selected(job.candidates[0]["cid"], True)
    assert jm.approve(
        job, split_review=lambda review: routes_jobs._build_unapproved_review(review, ""),
    ) is True
    assert job.status == jm.JobStatus.PENDING
    parked, = jm.registry.awaiting_review()
    assert jm.request_cancel(job) is True
    assert job_persistence.persist(parked) is False
    assert parked.set_selected(parked.candidates[0]["cid"], True) is None

    reviews = jm.registry.awaiting_review()
    assert reviews == [job]
    expected = [("picked", True), ("parked", False)]
    assert [(c["payload"]["album_id"], c["selected"])
            for c in job.candidates] == expected
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({kind: job_runs._resume_album_download})
    reviews = jm.registry.awaiting_review()
    assert len(reviews) == 1
    assert [(c["payload"]["album_id"], c["selected"])
            for c in reviews[0].candidates] == expected


def test_a_library_download_stopped_by_a_restart_returns_as_interrupted(
        monkeypatch, tmp_path):
    """A Library download the app stops comes back as interrupted, not
    failed or cancelled, keeping its log and stop time and flagging the picks
    it returned to the review for the Library page."""
    from qobuz_librarian.web import job_runs

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())

    job = Job(title=jm.RUN_TITLES["library"], kind="scan",
              execute_kind="library", status=JobStatus.RUNNING)
    assert _REAL_ADMIT(job) is True
    job.push_line("Stopped early. Artist · One and Artist · Two are back in "
                  "Library, ticked, to download again.")
    job.stopping_for_restart = True
    job.cancel_requested = True
    job.status = JobStatus.CANCELED
    jm._finish_task_phase(job)

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    hooks = []
    monkeypatch.setattr(jm, "_start_post_job_hook", hooks.append)
    jm.restore_jobs({"library": job_runs._resume_album_download})
    restored = jm.registry.get(job.id)
    assert restored.status is JobStatus.CANCELED
    assert restored.interrupted_by_restart
    assert [hook["status"] for hook in hooks] == ["interrupted"]
    assert restored.attention == "interrupted"
    assert restored.log_lines == job.log_lines
    assert restored.finished_at is not None


def test_a_parked_review_keeps_its_log_across_a_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())

    job = Job(title="New releases", kind="scan", execute_kind="new_releases",
              status=JobStatus.AWAITING_REVIEW)
    job.add_candidate("album", "Album", "Artist", payload={"id": 1})
    job.log_lines = ["Checked 40 artists.", "1 new release found."]
    jm.registry.add(job)
    assert job_persistence.persist(job) is True

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs({"new_releases": lambda job, args: (lambda j, chosen: None)})
    restored = jm.registry.get(job.id)
    assert restored.status is JobStatus.AWAITING_REVIEW
    assert restored.log_lines == job.log_lines


def test_an_upgrade_cut_short_by_a_restart_returns_only_unchanged_picks(
        monkeypatch, tmp_path):
    """An album upgraded just before the app was killed came back to the
    Upgrade review after the restart, where it could only be refused, and so
    did one put back from a backup on another filesystem, whose copied files
    have new inodes, at start-up or during the run. A put-back by the
    terminal before the app started left its pick out altogether, and one
    the app was deleting when it was killed, which the start-up fills in
    again, left the review and the saved results. Picks
    whose files still match return ticked, the ones the app put back sealed
    again; the changed one leaves the review and the saved results. A
    restart numbers the mounts afresh, so a refresh after it seals the same
    files differently, and reopening the review from Upgrade then unticked
    every returned pick. A pick whose files changed outside the app comes
    back unticked once a refresh seals it again."""
    import copy

    from qobuz_librarian import safefile
    from qobuz_librarian.library import backup as backup_mod
    from qobuz_librarian.library import candidate_premise, generation_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import flows, job_runs, lifespan, saved_reviews

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "UPGRADE_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup_mod, "_same_filesystem", lambda _a, _b: False)
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    monkeypatch.setattr(cfg, "LIBRARY_GENERATION_STATE_FILE",
                        tmp_path / "generation.json")
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())

    specs = []
    for title in ("Upgraded", "Not started", "Put back during the run",
                  "Put back by the terminal", "Killed while deleting"):
        album = cfg.MUSIC_ROOT / "Artist" / title
        album.mkdir(parents=True)
        (album / "01.flac").write_bytes(b"16-bit")
        (album / "02.flac").write_bytes(b"16-bit too")
        specs.append({"title": title, "artist": "Artist", "detail": "",
                      "payload": {"album_id": title, "album_dir": str(album),
                                  "_premise": candidate_premise.capture(
                                      "upgrade", album)}})
    generation = generation_state.commit_catalog_generation(
        generation_state.begin_attempt())["generation"]
    revision = generation_state.reserve_revision()
    assert upgrade_state._write_state({
        **upgrade_state._store.empty_state(), "complete": True,
        "generation": generation, "revision": revision, "candidates": specs,
        "quality_signature":
            saved_reviews._effective_upgrade_quality_signature()})
    assert generation_state.mark_output_current(
        "upgrade", generation=generation, revision=revision)
    run = Job(title=jm.RUN_TITLES["upgrade"], kind="scan",
              execute_kind="upgrade", status=JobStatus.RUNNING)
    for spec in specs:
        run.add_candidate("upgrade", spec["title"], "Artist",
                          payload=spec["payload"], selected=True)
    assert _REAL_ADMIT(run) is True
    (cfg.MUSIC_ROOT / "Artist" / "Upgraded" / "01.flac").write_bytes(b"24-bit")
    backup = backup_mod.backup_album_dir(
        cfg.MUSIC_ROOT / "Artist" / "Not started",
        expected_receipt=specs[1]["payload"]["_premise"]["receipt"])
    assert backup.complete
    jm.registry.add(run)
    during = cfg.MUSIC_ROOT / "Artist" / "Put back during the run"
    assert backup_mod.restore_upgrade_backup(
        backup_mod.backup_album_dir(during), during)
    terminal = cfg.MUSIC_ROOT / "Artist" / "Put back by the terminal"
    with monkeypatch.context() as patch:
        patch.setattr(backup_mod, "on_restored", None)
        assert backup_mod.restore_upgrade_backup(
            backup_mod.backup_album_dir(terminal), terminal)
    killed = cfg.MUSIC_ROOT / "Artist" / "Killed while deleting"

    class Killed(BaseException):
        pass

    delete_verified = safefile.delete_verified

    def killed_after_one(path, expected, **kwargs):
        delete_verified(path, expected, **kwargs)
        raise Killed

    with monkeypatch.context() as patch:
        patch.setattr(safefile, "delete_verified", killed_after_one)
        with pytest.raises(Killed):
            backup_mod.backup_album_dir(killed)
    assert len(list(killed.iterdir())) == 1

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    hooks = []
    monkeypatch.setattr(jm, "_start_post_job_hook", hooks.append)
    jm.restore_jobs(job_runs._RESUME_EXECUTE,
                    return_picks=flows.return_interrupted_picks,
                    finished_upgrades=[specs[0]["payload"]["album_dir"]])
    lifespan._put_back_albums()
    assert [(hook["status"], hook["summary"]) for hook in hooks] == [
        ("interrupted", jm.registry.get(run.id).error)]
    assert jm.registry.get(run.id).execute_args[job_persistence.ALBUMS_DONE] == 1
    review, = jm.registry.awaiting_review()
    assert [(c["title"], c["selected"]) for c in review.candidates] == [
        ("Not started", True), ("Put back during the run", True),
        ("Put back by the terminal", True), ("Killed while deleting", True)]
    for c in (*review.candidates[:2], review.candidates[3]):
        candidate_premise.validate(c)
    assert [c["title"] for c in upgrade_state.load()["candidates"]] == [
        "Not started", "Put back during the run", "Put back by the terminal",
        "Killed while deleting"]
    assert jm.registry.get(run.id).status is JobStatus.FAILED
    assert review.review_verb == "Upgrade"

    def renumbered(premise):
        premise = copy.deepcopy(premise)
        receipt = premise["receipt"]
        for identity in (*receipt["path_generations"],
                         *receipt["directory_generations"].values()):
            identity[6] += 1000
        return premise

    assert upgrade_state.reseal(renumbered)
    reopened = saved_reviews._review_job_from_current_saved_state("upgrade")
    assert reopened is review
    assert all(c["selected"] for c in review.candidates)
    for c in (*review.candidates[:2], review.candidates[3]):
        candidate_premise.validate(c)

    (cfg.MUSIC_ROOT / "Artist" / "Not started" / "01.flac").write_bytes(b"edited")
    assert upgrade_state.reseal(
        lambda premise: candidate_premise.capture("upgrade", premise["path"]))
    assert saved_reviews._review_job_from_current_saved_state("upgrade") is review
    assert [c["selected"] for c in review.candidates] == [
        False, True, False, True]


def test_a_library_run_cut_short_returns_missing_albums_its_downloads_touched(
        monkeypatch, tmp_path, tagged_flac):
    """A missing album is sealed over its whole artist folder, so after the
    app was killed mid-run every ticked album by an artist the run had
    already downloaded for was taken for changed on disk and lost from the
    review and the saved Library results. Those come back, sealed again
    unless the run's download may hold them; the downloaded one does not,
    and one whose folder changed outside the run still leaves. A later
    download of another album, from Search, made Start refuse them again."""
    from qobuz_librarian.library import (
        candidate_premise,
        generation_state,
        library_scan_state,
    )
    from qobuz_librarian.modes import process as process_mod
    from qobuz_librarian.web import flows, job_runs

    class HardStop(BaseException):
        pass

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "ARTIST_API_DELAY", 0)
    monkeypatch.setattr(cfg, "LIBRARY_GENERATION_STATE_FILE",
                        tmp_path / "generation.json")
    monkeypatch.setattr(cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library.json")
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()
    monkeypatch.setattr(jm, "registry", jm.JobRegistry())

    artists = {}
    picks = []
    for artist, albums in (("Artist", ("A", "B", "C")), ("Other", ("D",))):
        folder = cfg.MUSIC_ROOT / artist
        (folder / "Owned").mkdir(parents=True)
        (folder / "Owned" / "01.flac").write_bytes(b"x")
        tagged_flac(folder / "Owned" / "02.flac", title="Song", artist=artist)
        premise = candidate_premise.capture("missing", folder)
        rows = [{"artist": artist, "title": album,
                 "payload": {"album_id": album, "_artist_dir_path": str(folder),
                             "_premise": premise}} for album in albums]
        artists[artist] = {"fingerprint": "f", "candidates": rows,
                           "artist_id": artist, "catalog_ids": list(albums)}
        picks += rows
    generation = generation_state.commit_catalog_generation(
        generation_state.begin_attempt())["generation"]
    assert library_scan_state.save_kind(
        "missing", artists=artists, complete=True, generation=generation,
        revision=generation_state.reserve_revision())
    run = Job(title=jm.RUN_TITLES["library"], kind="scan",
              execute_kind="library", status=JobStatus.RUNNING)
    for row in picks:
        run.add_candidate("album", row["title"], row["artist"],
                          payload=row["payload"], selected=True)
    assert _REAL_ADMIT(run) is True

    def process_album(album, *_args, **_kwargs):
        if album["id"] == "B":
            raise HardStop
        album_dir = cfg.MUSIC_ROOT / "Artist" / "C (2001)"
        album_dir.mkdir()
        (album_dir / "01.flac").write_bytes(b"y")
        return {"imported": True, "n_ok": 1, "n_fail": 0,
                "result": "downloaded", "dir": str(album_dir)}

    monkeypatch.setattr(flows, "build_args", lambda: None)
    monkeypatch.setattr(flows, "get_album", lambda album_id, _token: {
        "id": album_id, "title": album_id})
    monkeypatch.setattr(flows.catalog, "find_edition_tracks",
                        lambda _album: (None, None, None, None))
    monkeypatch.setattr(process_mod, "process_album", process_album)
    monkeypatch.setattr(
        flows, "_refresh_after_local_album_change",
        lambda album, *_a, **_k: library_scan_state.remove_album(album["id"]))
    monkeypatch.setattr(flows, "prune_library_review_candidates",
                        lambda _album: 0)
    with pytest.raises(HardStop):
        flows.execute_albums(run, list(run.candidates), "token")
    (cfg.MUSIC_ROOT / "Other" / "D").mkdir()
    (cfg.MUSIC_ROOT / "Other" / "D" / "01.flac").write_bytes(b"z")

    monkeypatch.setattr(jm, "registry", jm.JobRegistry())
    jm.restore_jobs(job_runs._RESUME_EXECUTE,
                    return_picks=flows.return_interrupted_picks)
    review, = jm.registry.awaiting_review()
    assert [(c["title"], c["selected"]) for c in review.candidates] == [
        ("B", True), ("C", True)]
    assert [c["payload"]["album_id"]
            for entry in library_scan_state.kind_state("missing")[
                "artists"].values()
            for c in entry["candidates"]] == ["B", "C"]
    stale = candidate_premise.stale_candidate_ids(
        review.candidates, share_artist_captures=True)
    assert [c["title"] for c in review.candidates if c["cid"] in stale] == ["C"]

    # Another album of the artist that a download files beside them leaves
    # B startable too.
    album_dir = cfg.MUSIC_ROOT / "Artist" / "E (2005)"
    album_dir.mkdir()
    tagged_flac(album_dir / "01.flac", title="Other", artist="Artist")
    flows.reseal_past_album_import(album_dir)
    stale = candidate_premise.stale_candidate_ids(review.candidates)
    assert [c["title"] for c in review.candidates if c["cid"] in stale] == ["C"]

    # Lyrics the app writes into the artist's albums leave B startable,
    # saved for the next start too. A hand edit beside them does not.
    from qobuz_librarian.integrations import lyric_fetch
    from qobuz_librarian.library import lyrics

    monkeypatch.setattr(lyric_fetch, "AVAILABLE", True)
    monkeypatch.setattr(lyrics, "HAVE_LYRICS", True)
    monkeypatch.setattr(cfg, "LYRICS_FORMAT", "both")
    monkeypatch.setattr(cfg, "LYRIC_FETCH_STATE_FILE", tmp_path / "lyrics.json")
    for words, edit in (("[00:00.50]words", False), ("[00:00.50]more", True)):
        monkeypatch.setattr(lyric_fetch, "search_lyrics", lambda *_a, **_k: (
            words, "Lrclib", "synced", 1, 0))
        if edit:
            (cfg.MUSIC_ROOT / "Artist" / "Owned" / "01.flac").write_bytes(b"x2")
        flows.run_library_lyrics(
            Job(title="Lyrics scan"), rescan=True)
        stale = candidate_premise.stale_candidate_ids(review.candidates)
        assert [c["title"] for c in review.candidates
                if c["cid"] in stale] == (["B", "C"] if edit else ["C"])
        assert job_persistence.load_one(review.id)["candidates"] == (
            review.candidates)
    assert (cfg.MUSIC_ROOT / "Artist" / "Owned" / "02.lrc").read_text() == (
        "[00:00.50]words")


def test_a_provider_error_never_reaches_the_stored_job_record(
        monkeypatch, tmp_path):
    """A Qobuz call carries the account email and the auth token in its query
    string, so a failed call reported as the URL it called would put a working
    credential in the activity log and the archive. Records written before the
    masking existed are cleaned on the next start."""
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    leak = ("Cannot connect to https://example.test/api/user/login"
            "?user_id=nobody@example.test&user_auth_token=NOT-A-REAL-TOKEN-0000")
    job = Job(title="Download")
    job.push_line(leak)
    assert "NOT-A-REAL-TOKEN-0000" not in "\n".join(job.log_lines)
    assert "nobody@example.test" not in "\n".join(job.log_lines)

    job.status = JobStatus.FAILED
    assert _REAL_ADMIT(job) is True
    observer = sqlite3.connect(cfg.DATA_DIR / "jobs.db")
    try:
        observer.execute("UPDATE jobs SET error=?, summary=? WHERE id=?",
                         (leak, leak, job.id))
        observer.commit()
    finally:
        observer.close()

    assert job_persistence.scrub_stored_secrets() == 1
    observer = sqlite3.connect(cfg.DATA_DIR / "jobs.db")
    try:
        stored = observer.execute(
            "SELECT error, summary FROM jobs WHERE id=?", (job.id,)).fetchone()
    finally:
        observer.close()
    assert "NOT-A-REAL-TOKEN-0000" not in "".join(stored)
    assert "nobody@example.test" not in "".join(stored)


def test_album_imported_with_a_track_missing_stays_retryable_once_seen(
        monkeypatch, tmp_path):
    # Houses Of The Holy imported all but The Rain Song, which arrived damaged
    # twice. The job was saved as failed, and opening it cleared the only
    # marker that said a track was still missing.
    from qobuz_librarian.web import download_outcomes

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    job = Job(title="Houses Of The Holy", album_id="hoth")
    download_outcomes._mark_download_attention(job, {
        "result": "partial", "imported": True,
        "n_ok": 13, "n_fail": 0, "n_lossy": 1, "n_broken": 1,
    })
    job.finished_at = 2.0
    assert _REAL_ADMIT(job) is True
    observer = sqlite3.connect(cfg.DATA_DIR / "jobs.db")
    try:
        # Saved by an earlier version, before the count was recorded.
        observer.execute(
            "INSERT INTO jobs (id, title, album_id, status, attention, error, "
            "finished_at) VALUES ('oes', 'On Every Street', 'oes', 'failed', "
            "'partial', '1 track is still missing. Retry fetches it.', 1.0)")
        observer.commit()
    finally:
        observer.close()

    for job_id in (job.id, "oes"):
        assert job_persistence.acknowledge_attention(job_id, "partial")
    rows = {row["id"]: row for row in job_persistence.history_page(10, 0)}
    assert (rows[job.id]["status"], rows[job.id]["tracks_missing"]) == ("done", 1)
    assert (rows["oes"]["status"], rows["oes"]["tracks_missing"]) == ("failed", 1)


def test_download_that_imported_nothing_is_not_shown_as_imported(
        monkeypatch, tmp_path):
    # Two tracks of Blue Rev existed only lossy, so nothing was imported, yet
    # History read the count from the error and showed "Imported, 2 tracks
    # missing".
    from qobuz_librarian.web import download_outcomes

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    job_persistence._reset_for_tests()
    monkeypatch.setattr(job_persistence, "_disabled", False)
    job_persistence.init()

    job = Job(title="Blue Rev", album_id="br")
    download_outcomes._mark_download_attention(job, {
        "result": "partial", "imported": False, "n_ok": 12, "n_lossy_only": 2,
    })
    job.finished_at = 2.0
    assert _REAL_ADMIT(job) is True
    [row] = job_persistence.history_page(10, 0)
    assert (row["status"], row["tracks_missing"]) == ("failed", 0)
