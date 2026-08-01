from types import SimpleNamespace


def _upgrade_candidate(album_id="alb-1", title="Album"):
    return {
        "qobuz_album": {
            "id": album_id,
            "title": title,
            "artist": {"name": "Artist"},
            "maximum_bit_depth": 24,
            "maximum_sampling_rate": 96,
        },
        "existing_quality_label": "16-bit/44.1kHz",
        "target_quality_label": "24-bit/96kHz",
        "n_present": 10,
        "n_total": 10,
    }


def test_update_artist_replaces_only_that_artists_upgrade_candidates(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import upgrade_state

    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    artist_dir = tmp_path / "Artist"
    other_artist_dir = tmp_path / "Other Artist"
    artist_dir.mkdir()
    other_artist_dir.mkdir()
    upgrade_state.save(upgrade_state.RefreshResult(
        candidates=[
            {
                "title": "Old Album",
                "artist": "Artist",
                "detail": "old",
                "payload": {"album_id": "old"},
            },
            {
                "title": "Other Album",
                "artist": "Other Artist",
                "detail": "other",
                "payload": {"album_id": "other"},
            },
        ],
        artists_scanned=["Artist", "Other Artist"],
        errors={},
        complete=True,
    ))

    result = upgrade_state.update_artist(
        artist_dir,
        token="tok",
        args=SimpleNamespace(),
        capped={},
        hidden=None,
        scan_artist=lambda _ad: [_upgrade_candidate(album_id="new", title="New Album")],
    )

    assert [c["title"] for c in result.candidates] == ["New Album"]
    state = upgrade_state.load()
    assert [(c["artist"], c["title"]) for c in state["candidates"]] == [
        ("Other Artist", "Other Album"),
        ("Artist", "New Album"),
    ]
    assert state["artists_scanned"] == ["Artist", "Other Artist"]


def test_parallel_update_artist_keeps_both_upgrade_refreshes(
        tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import upgrade_state

    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    first_artist = tmp_path / "First Artist"
    second_artist = tmp_path / "Second Artist"
    first_artist.mkdir()
    second_artist.mkdir()
    upgrade_state.save(upgrade_state.RefreshResult([], [], {}, True))
    monkeypatch.setattr(
        upgrade_state, "artist_fingerprint", lambda artist_dir: artist_dir.name)

    real_load = upgrade_state.load
    load_barrier = threading.Barrier(2)

    def raced_load():
        state = real_load()
        try:
            load_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return state

    monkeypatch.setattr(upgrade_state, "load", raced_load)

    def refresh(artist_dir):
        return upgrade_state.update_artist(
            artist_dir,
            token="tok",
            args=SimpleNamespace(),
            capped={},
            hidden=None,
            scan_artist=lambda ad: [
                _upgrade_candidate(album_id=ad.name, title=f"{ad.name} Album")
            ],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(refresh, first_artist), pool.submit(refresh, second_artist)]
        for future in futures:
            future.result()

    state = real_load()
    assert sorted(c["artist"] for c in state["candidates"]) == [
        "First Artist",
        "Second Artist",
    ]


def test_refresh_for_artists_reuses_unchanged_upgrade_artist(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import upgrade_state

    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 4)
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    monkeypatch.setattr(upgrade_state, "artist_fingerprint",
                        lambda _path: "same", raising=False)

    calls = []
    first = upgrade_state.refresh_for_artists(
        [artist_dir],
        token="tok",
        args=SimpleNamespace(),
        capped={},
        hidden=None,
        scan_artist=lambda _ad: calls.append("scan") or [_upgrade_candidate()],
    )
    assert [c["title"] for c in first.candidates] == ["Album"]

    def fail_if_called(_artist_dir):
        raise AssertionError("unchanged artist should be reused")

    second = upgrade_state.refresh_for_artists(
        [artist_dir],
        token="tok",
        args=SimpleNamespace(),
        capped={},
        hidden=None,
        scan_artist=fail_if_called,
        skip_unchanged=True,
    )

    assert calls == ["scan"]
    assert [c["title"] for c in second.candidates] == ["Album"]

    monkeypatch.setattr(cfg, "STREAMRIP_QUALITY", 2)
    third = upgrade_state.refresh_for_artists(
        [artist_dir],
        token="tok",
        args=SimpleNamespace(),
        capped={},
        hidden=None,
        scan_artist=lambda _ad: calls.append("rescan") or [],
        skip_unchanged=True,
    )

    assert calls == ["scan", "rescan"]
    assert third.candidates == []


def test_full_save_preserves_newer_targeted_upgrade_refresh_with_same_fingerprint(
        tmp_path, monkeypatch):
    import time

    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import upgrade_state

    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    monkeypatch.setattr(upgrade_state, "artist_fingerprint",
                        lambda _path: "same", raising=False)
    stale_candidate = {
        "title": "Stale Album",
        "artist": "Artist",
        "detail": "old",
        "payload": {"album_id": "stale"},
    }
    upgrade_state.save(upgrade_state.RefreshResult(
        [stale_candidate],
        ["Artist"],
        {},
        True,
        {"Artist": "same"},
    ))

    refresh_started_at = time.time()
    time.sleep(0.001)
    full_result = upgrade_state.RefreshResult(
        [stale_candidate],
        ["Artist"],
        {},
        True,
        {"Artist": "same"},
    )
    upgrade_state.update_artist(
        artist_dir,
        token="tok",
        args=SimpleNamespace(),
        capped={},
        hidden=None,
        scan_artist=lambda _ad: [],
    )
    upgrade_state.save(
        full_result,
        preserve_concurrent=True,
        refresh_started_at=refresh_started_at,
    )

    assert upgrade_state.load()["candidates"] == []


def test_cancelled_refresh_keeps_last_complete_upgrade_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import upgrade_state

    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    upgrade_state.save(upgrade_state.RefreshResult(
        [{
            "title": "Album",
            "artist": "Artist",
            "detail": "old",
            "payload": {"album_id": "alb-1"},
        }],
        ["Artist"],
        {},
        True,
    ))

    result = upgrade_state.refresh_for_artists(
        [artist_dir],
        token="tok",
        args=SimpleNamespace(),
        capped={},
        scan_artist=lambda _ad: (_ for _ in ()).throw(
            AssertionError("cancelled refresh should not scan")),
        cancel_check=lambda: True,
    )

    state = upgrade_state.load()
    assert result.complete is False
    assert state["complete"] is True
    assert [c["title"] for c in state["candidates"]] == ["Album"]


def test_artist_error_keeps_last_complete_upgrade_state(
        tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.quality import upgrade_state

    monkeypatch.setattr(cfg, "UPGRADE_STATE_FILE", tmp_path / "upgrade.json")
    artist_dir = tmp_path / "Artist"
    artist_dir.mkdir()
    upgrade_state.save(upgrade_state.RefreshResult(
        [{
            "title": "Album",
            "artist": "Artist",
            "detail": "old",
            "payload": {"album_id": "alb-1"},
        }],
        ["Artist"],
        {},
        True,
    ))

    result = upgrade_state.refresh_for_artists(
        [artist_dir],
        token="tok",
        args=SimpleNamespace(),
        capped={},
        scan_artist=lambda _ad: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    state = upgrade_state.load()
    assert result.complete is False
    assert result.errors == {"Artist": "boom"}
    assert state["complete"] is True
    assert [c["title"] for c in state["candidates"]] == ["Album"]
