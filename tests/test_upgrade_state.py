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
        scan_artist=lambda _ad: [_upgrade_candidate(album_id="new", title="New Album")],
    )

    assert [c["title"] for c in result.candidates] == ["New Album"]
    state = upgrade_state.load()
    assert [(c["artist"], c["title"]) for c in state["candidates"]] == [
        ("Other Artist", "Other Album"),
        ("Artist", "New Album"),
    ]
    assert state["artists_scanned"] == ["Artist", "Other Artist"]


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
    # An artist that fails to scan leaves it the same way.
    result = upgrade_state.refresh_for_artists(
        [artist_dir], token="tok", args=SimpleNamespace(), capped={},
        scan_artist=lambda _ad: (_ for _ in ()).throw(RuntimeError("boom")))
    assert result.errors == {"Artist": "boom"}
    assert upgrade_state.load() == state
