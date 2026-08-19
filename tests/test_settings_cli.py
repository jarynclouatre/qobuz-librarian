from types import SimpleNamespace


def test_dry_run_shows_current_values_and_does_not_save(monkeypatch):
    from qobuz_librarian.modes import settings_cli
    from qobuz_librarian.web import settings_store

    monkeypatch.setattr(
        settings_store, "current",
        lambda: {
            "STREAMRIP_QUALITY": "3",
            "DOWNSAMPLE_KEEP_ORIGINALS": "keep",
            "PREFER_HIRES": True,
            "MIGRATE_MULTI_ARTIST": False,
            "DOWNSAMPLE_HIRES_ENABLED": False,
            "SUPPRESS_SINGLE_TRACK_GAPS": False,
            "LYRICS_ENABLED": True,
        },
    )
    saved = []
    monkeypatch.setattr(settings_store, "save", lambda values: saved.append(values))

    result = settings_cli.run_settings_mode(SimpleNamespace(dry_run=True))

    assert result == 0
    assert saved == []


def test_changed_answers_save_only_the_diff(monkeypatch):
    from qobuz_librarian.modes import settings_cli
    from qobuz_librarian.web import settings_store

    monkeypatch.setattr(
        settings_store, "current",
        lambda: {
            "STREAMRIP_QUALITY": "3",
            "DOWNSAMPLE_KEEP_ORIGINALS": "keep",
            "PREFER_HIRES": False,
            "MIGRATE_MULTI_ARTIST": False,
            "DOWNSAMPLE_HIRES_ENABLED": False,
            "SUPPRESS_SINGLE_TRACK_GAPS": False,
            "LYRICS_ENABLED": False,
        },
    )
    saved = []
    monkeypatch.setattr(
        settings_store, "save",
        lambda values: saved.append(values) or (True, []),
    )
    # Quality: pick "4". Downsample policy and every toggle: keep current
    # (Enter). PREFER_HIRES is the only toggle actually flipped.
    monkeypatch.setattr(settings_cli, "ask", lambda *_a, **_kw: "1")
    # confirm() is called once for the downsample policy, then once per
    # behaviour toggle in BEHAVIOR_FIELDS order. Only PREFER_HIRES flips.
    answers = iter([True, True, False, False, False, False])
    monkeypatch.setattr(
        settings_cli, "confirm",
        lambda *_a, **_kw: next(answers),
    )

    result = settings_cli.run_settings_mode(SimpleNamespace(dry_run=False))

    assert result == 0
    assert saved == [{"STREAMRIP_QUALITY": "4", "PREFER_HIRES": True}]


def test_closed_stdin_on_quality_cancels_without_saving(monkeypatch):
    from qobuz_librarian.modes import settings_cli
    from qobuz_librarian.web import settings_store

    monkeypatch.setattr(
        settings_store, "current",
        lambda: {
            "STREAMRIP_QUALITY": "3",
            "DOWNSAMPLE_KEEP_ORIGINALS": "keep",
            "PREFER_HIRES": False,
            "MIGRATE_MULTI_ARTIST": False,
            "DOWNSAMPLE_HIRES_ENABLED": False,
            "SUPPRESS_SINGLE_TRACK_GAPS": False,
            "LYRICS_ENABLED": False,
        },
    )
    saved = []
    monkeypatch.setattr(settings_store, "save", lambda values: saved.append(values))
    monkeypatch.setattr(settings_cli, "ask", lambda *_a, **_kw: None)

    result = settings_cli.run_settings_mode(SimpleNamespace(dry_run=False))

    assert result == 0
    assert saved == []
