from qobuz_librarian.quality.upgrade_state import RefreshResult


def _isolate_generation_files(monkeypatch, tmp_path):
    from qobuz_librarian import config as cfg

    monkeypatch.setattr(
        cfg,
        "LIBRARY_GENERATION_STATE_FILE",
        tmp_path / "generation.json",
    )
    monkeypatch.setattr(
        cfg,
        "LIBRARY_SCAN_STATE_FILE",
        tmp_path / "library.json",
    )
    monkeypatch.setattr(
        cfg,
        "UPGRADE_STATE_FILE",
        tmp_path / "upgrade.json",
    )
    monkeypatch.setattr(
        cfg,
        "DOWNSAMPLE_STATE_FILE",
        tmp_path / "downsample.json",
    )
    monkeypatch.setattr(
        cfg,
        "NEW_RELEASE_STATE_FILE",
        tmp_path / "new-releases.json",
    )


def _publish_library_generation(monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state, library_scan_state

    _isolate_generation_files(monkeypatch, tmp_path)
    attempt = generation_state.begin_attempt()
    publication = generation_state.commit_catalog_generation(attempt)
    revision = generation_state.reserve_revision()
    assert library_scan_state.save_kind(
        "missing",
        artists={},
        complete=True,
        generation=publication["generation"],
        revision=revision,
    ) == publication["generation"]
    assert generation_state.baseline_complete()
    return publication["generation"]


def test_failed_attempt_keeps_previous_complete_generation(monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state

    generation = _publish_library_generation(monkeypatch, tmp_path)
    attempt = generation_state.begin_attempt()

    assert generation_state.finish_attempt(
        attempt, "failed", "Qobuz was unavailable"
    )

    state = generation_state.load()
    assert state["generation"] == generation
    assert state["latest_attempt"]["status"] == "failed"
    assert generation_state.baseline_complete()


def test_first_generation_is_not_ready_when_library_snapshot_write_fails(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state, library_scan_state

    _isolate_generation_files(monkeypatch, tmp_path)
    attempt = generation_state.begin_attempt()
    publication = generation_state.commit_catalog_generation(attempt)
    revision = generation_state.reserve_revision()
    monkeypatch.setattr(library_scan_state, "_write_state", lambda _data: False)

    saved = library_scan_state.save_kind(
        "missing",
        artists={},
        complete=True,
        generation=publication["generation"],
        revision=revision,
    )

    assert saved is None
    assert generation_state.baseline_complete() is False
    assert generation_state.output_state("library")["status"] == "needs_refresh"


def test_restart_marks_committed_unpublished_library_attempt_incomplete(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state

    class Authority:
        @staticmethod
        def intact():
            return True

    _isolate_generation_files(monkeypatch, tmp_path)
    attempt = generation_state.begin_attempt()
    publication = generation_state.commit_catalog_generation(attempt)
    revision = generation_state.revision()

    assert publication is not None
    assert generation_state.library_publication_incomplete()
    assert generation_state.reconcile_interrupted_library_publication(
        Authority()
    ) is True

    state = generation_state.load()
    assert state["generation"] == publication["generation"]
    assert state["revision"] == revision + 1
    assert state["latest_attempt"]["status"] == "incomplete"
    assert state["outputs"]["library"]["status"] == "needs_refresh"
    assert generation_state.library_publication_incomplete(state) is False
    assert generation_state.reconcile_interrupted_library_publication(
        Authority()
    ) is False


def test_local_library_invalidation_is_not_mistaken_for_interrupted_publication(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state

    generation = _publish_library_generation(monkeypatch, tmp_path)
    assert generation_state.invalidate(
        ["library"],
        "A local download changed the Library view.",
    )

    state = generation_state.load()
    assert state["generation"] == generation
    assert state["latest_attempt"]["status"] == "complete"
    assert state["outputs"]["library"]["status"] == "stale"
    assert generation_state.library_publication_incomplete(state) is False


def test_stale_library_output_keeps_its_published_snapshot_available(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state

    _publish_library_generation(monkeypatch, tmp_path)
    assert generation_state.library_snapshot_available()

    assert generation_state.invalidate(
        ["library"], "The local album changed."
    )

    assert generation_state.library_snapshot_available()
    assert generation_state.baseline_complete() is False


def test_failed_library_snapshot_restores_previous_complete_generation(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state, library_scan_state

    previous_generation = _publish_library_generation(monkeypatch, tmp_path)
    attempt = generation_state.begin_attempt()
    publication = generation_state.commit_catalog_generation(
        attempt,
        expected_revision=generation_state.revision(),
    )
    revision = generation_state.reserve_revision()
    monkeypatch.setattr(library_scan_state, "_write_state", lambda _data: False)

    assert library_scan_state.save_kind(
        "missing",
        artists={},
        complete=True,
        generation=publication["generation"],
        revision=revision,
    ) is None
    assert generation_state.abort_catalog_generation(
        publication,
        "Library snapshot could not be saved",
    )

    state = generation_state.load()
    assert state["generation"] == previous_generation
    assert state["latest_attempt"]["status"] == "failed"
    assert generation_state.baseline_complete()


def test_new_generation_refuses_to_publish_over_a_newer_revision(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state

    previous_generation = _publish_library_generation(monkeypatch, tmp_path)
    attempt = generation_state.begin_attempt()
    scan_revision = generation_state.revision()
    assert generation_state.invalidate(
        ["upgrade"],
        "A newer local operation changed one saved view.",
    )

    assert generation_state.commit_catalog_generation(
        attempt,
        expected_revision=scan_revision,
    ) is None
    assert generation_state.current_generation() == previous_generation


def test_revision_reservations_are_unique_across_processes(
        monkeypatch, tmp_path):
    import multiprocessing
    import time

    from qobuz_librarian.library import generation_state

    _isolate_generation_files(monkeypatch, tmp_path)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    worker_count = 8
    reservations_per_worker = 30

    def reserve_many():
        original_write = generation_state._write

        def delayed_write(data):
            time.sleep(0.002)
            return original_write(data)

        generation_state._write = delayed_write
        start.wait()
        results.put([
            generation_state.reserve_revision()
            for _ in range(reservations_per_worker)
        ])

    workers = [context.Process(target=reserve_many)
               for _ in range(worker_count)]
    for worker in workers:
        worker.start()
    start.set()
    batches = [results.get(timeout=30) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)

    assert [worker.exitcode for worker in workers] == [0] * worker_count
    values = [value for batch in batches for value in batch]
    expected = worker_count * reservations_per_worker
    assert sorted(values) == list(range(1, expected + 1))
    assert generation_state.revision() == expected


def test_older_output_revision_cannot_replace_a_newer_one(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state

    _isolate_generation_files(monkeypatch, tmp_path)
    attempt = generation_state.begin_attempt()
    publication = generation_state.commit_catalog_generation(attempt)
    older = generation_state.reserve_revision()
    newer = generation_state.reserve_revision()

    assert generation_state.mark_output_current(
        "upgrade",
        generation=publication["generation"],
        revision=newer,
    )
    assert generation_state.mark_output_current(
        "upgrade",
        generation=publication["generation"],
        revision=older,
    ) is False
    assert generation_state.output_state("upgrade")["revision"] == newer


def test_targeted_output_update_preserves_unrelated_stale_status(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state

    generation = _publish_library_generation(monkeypatch, tmp_path)
    initial = generation_state.reserve_revision()
    assert generation_state.mark_output_current(
        "upgrade",
        generation=generation,
        revision=initial,
    )
    reason = "Another artist still needs a refresh."
    assert generation_state.mark_output_status(
        "upgrade",
        "stale",
        reason=reason,
    )

    targeted = generation_state.reserve_revision()
    assert generation_state.mark_output_current(
        "upgrade",
        generation=generation,
        revision=targeted,
        preserve_noncurrent=True,
    )

    output = generation_state.output_state("upgrade")
    assert output["status"] == "stale"
    assert output["complete"] is False
    assert output["reason"] == reason
    assert output["revision"] == targeted

    full = generation_state.reserve_revision()
    assert generation_state.mark_output_current(
        "upgrade",
        generation=generation,
        revision=full,
    )
    assert generation_state.output_is_current("upgrade")


def test_targeted_new_release_merge_does_not_clear_stale_baseline(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state, new_releases

    generation = _publish_library_generation(monkeypatch, tmp_path)
    assert new_releases.seed_baseline(
        {"artist-id": ["old-album"]},
        generation=generation,
    )
    assert generation_state.mark_output_status(
        "new_releases",
        "stale",
        reason="Another artist changed without an exact identity.",
    )

    assert new_releases.note_owned_album({
        "id": "new-album",
        "artist": {"id": "artist-id"},
    })

    saved = new_releases.load()
    output = generation_state.output_state("new_releases")
    assert saved["seen"]["artist-id"] == ["old-album", "new-album"]
    assert output["revision"] == saved["revision"]
    assert output["status"] == "stale"


def test_targeted_artist_merges_do_not_clear_other_stale_artists(
        monkeypatch, tmp_path):
    from types import SimpleNamespace

    from qobuz_librarian.library import downsample_state, generation_state
    from qobuz_librarian.quality import upgrade_state

    generation = _publish_library_generation(monkeypatch, tmp_path)
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    assert upgrade_state.save(
        upgrade_state.RefreshResult(
            [],
            ["Other Artist"],
            {},
            True,
            {"Other Artist": "before"},
            quality_signature=upgrade_state.quality_signature(),
        ),
        generation=generation,
    )
    assert downsample_state.save(
        downsample_state.RefreshResult(
            [], ["Other Artist"], {}, True, {"Other Artist": "before"}
        ),
        generation=generation,
    )
    for surface in ("upgrade", "downsample"):
        assert generation_state.mark_output_status(
            surface,
            "stale",
            reason="Another artist still needs a refresh.",
        )

    assert upgrade_state.update_artist(
        artist_dir,
        token="token",
        args=SimpleNamespace(),
        capped={},
        scan_artist=lambda _artist: [],
    ).complete
    assert downsample_state.update_artist(
        artist_dir,
        scan_artist=lambda _artist: [],
    ).complete

    for surface, saved in (
        ("upgrade", upgrade_state.load()),
        ("downsample", downsample_state.load()),
    ):
        output = generation_state.output_state(surface)
        assert output["status"] == "stale"
        assert output["revision"] == saved["revision"]


def test_partial_new_release_update_advances_its_authoritative_revision(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state, new_releases

    generation = _publish_library_generation(monkeypatch, tmp_path)
    assert new_releases.seed_baseline(
        {"artist": ["old"]},
        generation=generation,
    )
    previous_revision = new_releases.load()["revision"]

    assert new_releases.mark_run(
        {"artist": ["old", "new"]},
        complete=False,
    )

    state = new_releases.load()
    output = generation_state.output_state("new_releases")
    assert state["revision"] > previous_revision
    assert output["revision"] == state["revision"]
    assert generation_state.output_is_current("new_releases")
    assert new_releases.is_baseline_complete()


def test_upgrade_zero_is_current_only_for_its_generation_and_policy(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state
    from qobuz_librarian.quality import upgrade_state
    from qobuz_librarian.web import app as webapp

    generation = _publish_library_generation(monkeypatch, tmp_path)
    result = RefreshResult(
        [],
        ["Artist"],
        {},
        True,
        {"Artist": "fingerprint"},
        quality_signature=upgrade_state.quality_signature(),
    )
    assert upgrade_state.save(result, generation=generation)

    current = webapp._upgrade_state_summary()
    assert current["complete"] is True
    assert current["count"] == 0
    assert current["stale"] is False

    monkeypatch.setattr(
        webapp,
        "_effective_upgrade_quality_signature",
        lambda: "changed-policy",
    )
    policy_stale = webapp._upgrade_state_summary()
    assert policy_stale["complete"] is True
    assert policy_stale["count"] == 0
    assert policy_stale["stale"] is True

    attempt = generation_state.begin_attempt()
    generation_state.commit_catalog_generation(attempt)
    older_generation = webapp._upgrade_state_summary()
    assert older_generation["complete"] is False
    assert older_generation["stale"] is True


def test_long_upgrade_publish_preserves_newer_artist_revision(
        monkeypatch, tmp_path):
    from qobuz_librarian.library import generation_state
    from qobuz_librarian.quality import upgrade_state

    generation = _publish_library_generation(monkeypatch, tmp_path)
    scan_started_revision = generation_state.revision()
    local = RefreshResult(
        [{"artist": "Artist", "title": "Local", "payload": {"album_id": "local"}}],
        ["Artist"],
        {},
        True,
        {"Artist": "local-fingerprint"},
        quality_signature=upgrade_state.quality_signature(),
    )
    assert upgrade_state.save(local, generation=generation)
    long_scan = RefreshResult(
        [{"artist": "Artist", "title": "Old crawl", "payload": {"album_id": "old"}}],
        ["Artist"],
        {},
        True,
        {"Artist": "old-fingerprint"},
        quality_signature=upgrade_state.quality_signature(),
    )

    assert upgrade_state.save(
        long_scan,
        generation=generation,
        preserve_concurrent=True,
        refresh_started_revision=scan_started_revision,
    )

    state = upgrade_state.load()
    assert [candidate["title"] for candidate in state["candidates"]] == ["Local"]
    assert state["fingerprints"]["Artist"] == "local-fingerprint"
