

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
