from types import SimpleNamespace


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
    # ask() takes the quality, then one answer per behaviour toggle in
    # BEHAVIOR_FIELDS order; confirm() takes the downsample policy.
    answers = iter(["1", "y", "", "", "", ""])
    monkeypatch.setattr(settings_cli, "ask", lambda *_a, **_kw: next(answers))
    monkeypatch.setattr(settings_cli, "confirm", lambda *_a, **_kw: True)

    result = settings_cli.run_settings_mode(SimpleNamespace(dry_run=False))

    assert result == 0
    assert saved == [{"STREAMRIP_QUALITY": "4", "PREFER_HIRES": True}]


def test_cd_quality_marks_the_settings_it_switches_off():
    from qobuz_librarian.web import settings_store

    values = {"STREAMRIP_QUALITY": "2",
              "DOWNSAMPLE_HIRES_ENABLED": True, "PREFER_HIRES": True}
    assert set(settings_store.inert_behaviour_notes(values)) == {
        "DOWNSAMPLE_HIRES_ENABLED", "PREFER_HIRES"}
    assert settings_store.inert_behaviour_notes({**values, "PREFER_HIRES": False}) \
        .keys() == {"DOWNSAMPLE_HIRES_ENABLED"}
    assert settings_store.inert_behaviour_notes({**values, "STREAMRIP_QUALITY": "3"}) == {}


def test_a_typo_does_not_save_delete_originals(monkeypatch):
    # "No" here means delete the hi-res originals from now on, so an answer
    # the prompt does not understand must not be read as one. The first
    # downsample already re-asks; Settings saved "delete" for anything but y.
    from qobuz_librarian.modes import settings_cli
    from qobuz_librarian.ui_cli import prompts

    answers = iter(["maybe", "y"])
    asked = []

    def ask(prompt, **_kwargs):
        asked.append(prompt)
        return next(answers)

    monkeypatch.setattr(prompts, "ask", ask)
    assert settings_cli._pick_downsample_policy("keep") == "keep"
    assert len(asked) == 2
