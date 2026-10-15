import re
from pathlib import Path
from types import SimpleNamespace

import pytest


def _bind_current_upgrade(monkeypatch, upgrade, state):
    state["generation"] = 1
    state["revision"] = 2
    state["quality_signature"] = upgrade.upgrade_state.quality_signature()
    monkeypatch.setattr(
        upgrade.generation_state,
        "load",
        lambda: {
            "generation": 1,
            "outputs": {
                "upgrade": {
                    "generation": 1,
                    "revision": 2,
                    "status": "current",
                    "complete": True,
                },
            },
        },
    )


def test_upgrade_walk_refuses_a_changed_saved_candidate(
        monkeypatch, tmp_path, caplog):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library.candidate_premise import capture
    from qobuz_librarian.modes import upgrade

    root = tmp_path / "music"
    album_dir = root / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    track = album_dir / "01.flac"
    track.write_bytes(b"reviewed audio")
    monkeypatch.setattr(cfg, "MUSIC_ROOT", root)
    premise = capture("upgrade", album_dir)
    state = {
        "complete": True,
        "candidates": [{
            "artist": "Artist",
            "title": "Album",
            "detail": "CD -> 24-bit / 96 kHz",
            "payload": {
                "album_id": "alb-1",
                "album_dir": str(album_dir),
                "_premise": premise,
                "title_similarity": 1.0,
                "needed_edition_swap": False,
            },
        }],
    }
    _bind_current_upgrade(monkeypatch, upgrade, state)
    monkeypatch.setattr(upgrade.upgrade_state, "load", lambda: state)
    monkeypatch.setattr(upgrade.hidden_mod, "load", lambda: {})
    monkeypatch.setattr(
        upgrade,
        "get_album",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a stale candidate must be refused before Qobuz")
        ),
    )
    monkeypatch.setattr(
        upgrade,
        "process_album",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a stale candidate must not reach file work")
        ),
    )
    monkeypatch.setattr(upgrade.time, "sleep", lambda *_args: None)

    # A dry run lists every candidate without asking, so it runs unattended.
    def no_terminal(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    preview = SimpleNamespace(
        yes=False, auto_safe=False, dry_run=True, consolidate=True)
    assert upgrade.run_upgrade_walk_mode(preview, "token") == 0

    track.write_bytes(b"changed after the Library review")

    args = SimpleNamespace(
        yes=True,
        auto_safe=False,
        dry_run=False,
        consolidate=True,
    )
    with caplog.at_level("INFO", logger="qobuz_librarian"):
        result = upgrade.run_upgrade_walk_mode(args, "token")

    assert result == upgrade.EXIT_GENERAL


def _album_gaps_over_one_album(monkeypatch, tmp_path, status, answers):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library.discovery import DirMatch
    from qobuz_librarian.modes import artist, walk

    monkeypatch.setattr(cfg, "ALBUM_WALK_SEEN_FILE", tmp_path / "seen.txt")
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    tracks = [{"title": "One", "track_number": 1},
              {"title": "Two", "track_number": 2}]
    match = DirMatch(
        status, album_dir,
        qobuz_album={"id": "1", "title": "Album", "tracks": {"items": tracks}},
        missing=tracks[1:] if status == "partial" else [],
        present=tracks[:1] if status == "partial" else tracks,
    )
    monkeypatch.setattr(walk, "list_library_artists",
                        lambda **_k: [album_dir.parent])
    for module in (walk, artist):
        monkeypatch.setattr(module, "list_artist_album_dirs",
                            lambda _d: [album_dir])
        monkeypatch.setattr(module, "_flush_stdin", lambda: None)
    monkeypatch.setattr(walk, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(artist, "resolve_artist", lambda *_a: (None, None))
    monkeypatch.setattr(artist, "match_album_dir", lambda *_a, **_k: match)
    monkeypatch.setattr(artist.time, "sleep", lambda *_a: None)
    replies = iter(answers)

    def _input(_prompt=""):
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", _input)
    return cfg.ALBUM_WALK_SEEN_FILE


def test_album_gaps_does_not_remember_a_prompt_nobody_answered(
        monkeypatch, tmp_path):
    """A closed input read as "no" at the fill prompt, so the album was saved
    as skipped and every later walk passed over it without asking."""
    from qobuz_librarian.modes import walk

    seen = _album_gaps_over_one_album(monkeypatch, tmp_path, "partial", [""])
    args = SimpleNamespace(yes=False, dry_run=False, consolidate=False,
                           prefer_hires=True)

    code = walk.run_album_walk_mode(args, "tok")

    assert not seen.exists()
    assert code == walk.EXIT_GENERAL


def test_album_gaps_offers_a_remembered_album_again_once_its_tracks_change(
        monkeypatch, tmp_path):
    """An album Gap Fill had found complete was left out of every later walk,
    even after a track went missing from its folder."""
    from qobuz_librarian.modes import artist, walk

    seen = _album_gaps_over_one_album(monkeypatch, tmp_path, "complete",
                                      ["", "", ""])
    matched = []
    match = artist.match_album_dir(None)
    monkeypatch.setattr(artist, "match_album_dir",
                        lambda d, *_a, **_k: matched.append(d) or match)
    args = SimpleNamespace(yes=False, dry_run=False, consolidate=False,
                           prefer_hires=True)
    album_dir = tmp_path / "Artist" / "Album"
    (album_dir / "01 - One.flac").write_bytes(b"")

    walk.run_album_walk_mode(args, "tok")
    walk.run_album_walk_mode(args, "tok")
    assert matched == [album_dir]

    (album_dir / "01 - One.flac").unlink()
    walk.run_album_walk_mode(args, "tok")
    assert matched == [album_dir, album_dir]
    assert seen.exists()


def test_ctrl_c_in_a_library_walk_keeps_the_queue_without_asking(
        monkeypatch, tmp_path):
    """Ctrl-C at "Download the queue now?" was followed by "Process them before
    exiting? [Y/n]", so the next Enter started the downloads."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import walk
    from qobuz_librarian.queue import persistence

    monkeypatch.setattr(cfg, "WALK_SEEN_FILE", tmp_path / "seen.txt")
    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(walk, "list_library_artists",
                        lambda **_k: [tmp_path / "Artist"])
    monkeypatch.setattr(walk, "clear_scan_caches", lambda: None)
    monkeypatch.setattr(walk, "_flush_stdin", lambda: None)

    def gap_fill(*_a, shared_queue, **_k):
        shared_queue.append({"label": "Album"})
        return [], set(), set(), {}, None, []

    monkeypatch.setattr(walk, "run_artist_gap_fill", gap_fill)
    monkeypatch.setattr(
        walk, "_execute_download_queue",
        lambda *_a, **_k: pytest.fail("a Ctrl-C started the downloads"))
    saved = []
    monkeypatch.setattr(persistence, "save_pending_queue",
                        lambda items, mode: saved.append(len(items)))
    replies = iter(["", "y", ""])

    def _input(prompt=""):
        if "Download the queue now" in prompt:
            raise KeyboardInterrupt
        return next(replies)

    monkeypatch.setattr("builtins.input", _input)
    args = SimpleNamespace(yes=False, dry_run=False, consolidate=False,
                           no_catalog=True)

    assert walk.run_walk_queued_mode(args, "tok") == walk.EXIT_INTERRUPT
    assert saved


def test_ctrl_c_in_a_second_artist_run_keeps_both_runs_approvals(
        monkeypatch, tmp_path):
    """A queue left by an earlier Artist mode run stopped the next run saving
    its own approvals, so a Ctrl-C dropped them while announcing the old
    queue as kept."""
    from qobuz_librarian import config as cfg
    from qobuz_librarian.modes import artist
    from qobuz_librarian.queue import journal, persistence
    from qobuz_librarian.queue.builder import _build_queue_item

    root = tmp_path / "music" / "Artist"
    dirs = [root / name for name in ("Earlier", "Today", "Later")]
    for d in dirs:
        d.mkdir(parents=True)
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path / "music")
    monkeypatch.setattr(cfg, "ARTIST_API_DELAY", 0)

    def album(d):
        return {"id": d.name, "title": d.name,
                "tracks": {"items": [{"id": 1}, {"id": 2}]}}

    def queued(d):
        return _build_queue_item(
            album=album(d), album_dir=d, label="Artist", missing=[{"id": 2}],
            present=[{"id": 1}], upgrade_only=False, auto_upgrade=False)

    journal.save_pending_queue([queued(dirs[0])], mode="artist")

    monkeypatch.setattr(artist, "list_artist_album_dirs", lambda _d: dirs)
    monkeypatch.setattr(artist, "resolve_artist", lambda *_a: (None, None))
    monkeypatch.setattr(artist, "_admit_new_queue_items", lambda *_a: None)
    monkeypatch.setattr(artist, "_flush_stdin", lambda: None)
    monkeypatch.setattr(artist, "match_album_dir", lambda d, *_a, **_k:
                        SimpleNamespace(status="partial", qobuz_album=album(d),
                                        existing=[{"id": 1}], present=[{"id": 1}],
                                        missing=[{"id": 2}]))
    replies = iter(["y", "y"])

    def _input(prompt=""):
        reply = next(replies, None)
        if reply is None:
            raise KeyboardInterrupt
        return reply

    monkeypatch.setattr("builtins.input", _input)
    args = SimpleNamespace(yes=False, dry_run=False, consolidate=False,
                           prefer_hires=False, include_bonus=False,
                           no_upgrade=False)

    with pytest.raises(KeyboardInterrupt):
        artist.run_artist_gap_fill("Artist", root, args, "tok", fresh=True)

    kept, mode, _saved_at = persistence.load_pending_queue()
    assert mode == "artist"
    assert [item["album_dir"] for item in kept] == dirs[:2]


def test_a_terminal_download_leaves_the_living_library_review(
        tmp_path, monkeypatch):
    """The living review is held in memory by whichever process serves the web
    UI, so a terminal download could not reach it: the same page counted the
    album's tracks as on disk while the review still offered to download it.
    """
    from qobuz_librarian import config as cfg
    from qobuz_librarian.library import generation_state, library_scan_state
    from qobuz_librarian.web import flows, job_persistence
    from qobuz_librarian.web import jobs as job_mgr

    # The suite runs without the job archive, so review saves report failure
    # and every mutation rolls back.
    monkeypatch.setattr(
        job_persistence,
        "persist_review_mutation",
        lambda _job, mutate: (True, mutate()),
    )
    monkeypatch.setattr(
        cfg, "LIBRARY_GENERATION_STATE_FILE", tmp_path / "generation.json")
    monkeypatch.setattr(
        cfg, "LIBRARY_SCAN_STATE_FILE", tmp_path / "library.json")

    attempt = generation_state.begin_attempt()
    generation = generation_state.commit_catalog_generation(
        attempt)["generation"]
    assert library_scan_state.save_kind(
        "missing",
        artists={
            "Artist": {
                "fingerprint": "baseline",
                "candidates": [{
                    "artist": "Artist",
                    "title": "Album",
                    "payload": {"album_id": "album-id"},
                }],
                "artist_id": "artist-id",
                "catalog_ids": ["album-id"],
            },
        },
        complete=True,
        generation=generation,
        revision=generation_state.reserve_revision(),
    )

    review = job_mgr.Job(title="Library scan")
    review.kind = "scan"
    review.execute_kind = "library"
    review.status = job_mgr.JobStatus.AWAITING_REVIEW
    review._execute_fn = lambda _job, _chosen: None
    review.add_candidate(
        "album", "Album", "Artist", payload={"album_id": "album-id"})
    review.add_candidate(
        "album", "Other", "Artist", payload={"album_id": "other-id"})
    job_mgr.registry.add(review)

    assert library_scan_state.remove_album("album-id")
    assert flows.apply_pending_review_removals() == 1

    assert [c["payload"]["album_id"] for c in review.candidates] == ["other-id"]
    assert generation_state.pending_review_removals() == []


def test_a_terminal_download_lands_in_the_activity_record(monkeypatch):
    """Queue and History read the job archive, which only the web writes, so a
    finished terminal download left no trace on the activity record at all,
    and terminal runs never reached POST_JOB_HOOK."""
    from qobuz_librarian import post_job_hook
    from qobuz_librarian.queue import executor
    from qobuz_librarian.web import download_outcomes, job_persistence

    monkeypatch.setattr(job_persistence, "_disabled", False)
    hooks = []
    monkeypatch.setattr(post_job_hook, "run", hooks.append)

    executor._record_terminal_downloads([{
        "album": {
            "id": "terminal-album",
            "title": "GAK",
            "artist": {"name": "Aphex Twin"},
        },
        "imported": True,
        "n_ok": 4,
        "elapsed_s": 18,
    }])

    rows = [
        row for row in job_persistence.history_page(50, 0, bulk=False)
        if row["album_id"] == "terminal-album"
    ]
    assert len(rows) == 1
    assert rows[0]["artist"] == "Aphex Twin"
    assert rows[0]["status"] == "done"
    assert rows[0]["finished_at"] - rows[0]["created_at"] == 18

    # A result that stayed below the target quality is recorded the way the
    # web records it, so History shows it with its dot and no Retry.
    executor._record_terminal_downloads([{
        "album": {"id": "terminal-quality", "title": "GAK",
                  "artist": {"name": "Aphex Twin"}},
        "result": "partial", "imported": True, "n_ok": 4,
        "quality_verdict": {
            "under": True, "target": (24, 96000), "served": (16, 44100),
            "retried": True, "recovered": False,
        },
    }])
    row = next(
        row for row in job_persistence.history_page(50, 0, bulk=False)
        if row["album_id"] == "terminal-quality"
    )
    assert (row["status"], row["attention"]) == ("failed", "quality")
    assert row["execute_args"]["retry_disabled"]
    saved = job_persistence.load_one(row["id"])
    assert saved["quality_shortfall"]["served"] == [16, 44100]
    assert job_persistence.acknowledge_attention(row["id"], "quality")

    # An album Qobuz could not serve whole imported nothing; History and the
    # hook say so in the web's words instead of counting landed tracks.
    short = {"result": "incomplete", "imported": False, "n_ok": 8, "n_fail": 2}
    executor._record_terminal_downloads([{
        **short,
        "album": {"id": "terminal-short", "title": "Syro",
                  "artist": {"name": "Aphex Twin"}},
    }])
    row = next(
        row for row in job_persistence.history_page(50, 0, bulk=False)
        if row["album_id"] == "terminal-short"
    )
    assert row["status"] == "failed"
    assert row["summary"] == download_outcomes._summarize_download_result(short)
    assert row["error"] == download_outcomes.download_error(
        short, job_controls=False)
    assert (hooks[-1]["summary"], hooks[-1]["error"]) == (
        row["summary"], row["error"])

    # A download stopped with Ctrl-C, and an Upgrade, Repair or Downsample
    # run, are recorded and reported as their web jobs are.
    executor._record_terminal_downloads([{
        "album": {"id": "terminal-stopped", "title": "Drukqs",
                  "artist": {"name": "Aphex Twin"}},
        "result": "interrupted",
    }])
    post_job_hook.report_terminal_run(
        kind="upgrade", title="Upgrade run", status="failed",
        summary="1 album upgraded, 1 couldn't be upgraded.",
        error="Finished with 1 error.", started_at=0)
    stopped = next(
        row for row in job_persistence.history_page(50, 0, bulk=False)
        if row["album_id"] == "terminal-stopped")
    card = job_persistence.history_page(50, 0, bulk=True)[0]
    assert stopped["status"] == "canceled"
    assert (card["execute_kind"], card["status"]) == ("upgrade", "failed")
    assert [(h["id"], h["kind"], h["status"]) for h in hooks[-2:]] == [
        (stopped["id"], "download", "canceled"),
        (card["id"], "upgrade", "failed")]
    assert len(hooks) == 5

    # A whole --no-import download is done, kept in staging.
    executor._refresh_review_state_after_downloads([{
        "album": {"id": "terminal-kept", "title": "Syro",
                  "artist": {"name": "Aphex Twin"}},
        "result": "downloaded", "imported": False, "n_ok": 12,
    }], None, SimpleNamespace(no_import=True))
    kept = next(
        row for row in job_persistence.history_page(50, 0, bulk=False)
        if row["album_id"] == "terminal-kept")
    assert kept["status"] == hooks[-1]["status"] == "done"


def test_ctrl_c_exits_with_the_interrupt_code(monkeypatch):
    """A typo, a real failure and Ctrl-C all exited 1, so nothing reading the
    exit code could tell "the operator stopped it" from "it broke"."""
    from qobuz_librarian import cli
    from qobuz_librarian.ui_cli import errors

    assert errors.EXIT_INTERRUPT == 130  # 128 + SIGINT, the shell convention

    def _interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_maybe_drop_privileges", lambda: None)
    monkeypatch.setattr(cli, "_check_staging_occupied", lambda: None)
    monkeypatch.setattr(cli, "main", _interrupted)

    with pytest.raises(SystemExit) as exit_info:
        cli._entry()

    assert exit_info.value.code == errors.EXIT_INTERRUPT
