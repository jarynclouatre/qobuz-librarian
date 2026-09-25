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
    # Once the output goes stale, its last published results stay readable.
    assert generation_state.invalidate(["library"], "The local album changed.")
    assert generation_state.library_snapshot_available()
    assert generation_state.baseline_complete() is False


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
