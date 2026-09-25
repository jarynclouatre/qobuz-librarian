from qobuz_librarian.library import hidden


def test_hide_is_scoped_durable_and_restorable(monkeypatch, tmp_path):
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", tmp_path / "h.json")
    assert hidden.hide(hidden.SCOPE_MISSING, [("Portishead", "Dummy", "1994")]) == 1
    # Another edition keys to the same album, so it joins that entry rather than
    # opening a second one, but it is a second row off the user's review, and
    # the counts he reads have to say so.
    assert hidden.hide(hidden.SCOPE_MISSING,
                       [("Portishead", "Dummy (Remaster)", None)]) == 1
    assert hidden.count(hidden.SCOPE_MISSING) == 2
    # Dismissing the same row twice still isn't news.
    assert hidden.hide(hidden.SCOPE_MISSING,
                       [("Portishead", "Dummy (Remaster)", None)]) == 0

    store = hidden.load()  # round-trips through disk
    assert len(store[hidden.SCOPE_MISSING]) == 1, "one album, not one per edition"
    assert hidden.is_hidden(hidden.SCOPE_MISSING, "Portishead", "Dummy", store)
    # A missing-hide leaves the upgrade scope untouched.
    assert not hidden.is_hidden(hidden.SCOPE_UPGRADE, "Portishead", "Dummy", store)

    groups = hidden.hidden_by_artist(hidden.SCOPE_MISSING)
    assert len(groups) == 1
    assert groups[0]["artist"] == "Portishead"
    assert groups[0]["rows"] == 2
    assert [(a["title"], a["year"]) for a in groups[0]["albums"]] == [("Dummy", "1994")]
    # The page says what else the one Restore button will bring back, with
    # each other edition's own year so two same-named editions stay tellable
    # apart.
    assert groups[0]["albums"][0]["others"] == [{"title": "Dummy (Remaster)", "year": ""}]

    assert hidden.restore(hidden.SCOPE_MISSING, ["Portishead"]) == 2
    assert hidden.count(hidden.SCOPE_MISSING) == 0
    # A self-titled album from another year is a different album.
    hidden.hide(hidden.SCOPE_MISSING, [("Weezer", "Weezer", "1994")])
    assert not hidden.is_hidden(
        hidden.SCOPE_MISSING, "Weezer", "Weezer", hidden.load(), year="2001")


def test_corrupt_store_is_preserved_not_silently_wiped(monkeypatch, tmp_path):
    # A corrupt store must NOT be silently overwritten by the next save(),
    # that would destroy a curated hide list.
    p = tmp_path / "h.json"
    p.write_text('{"missing": {"a|b": {"artist": "A"}}, THIS IS BROKEN',
                 encoding="utf-8")
    monkeypatch.setattr("qobuz_librarian.config.HIDDEN_FILE", p)

    hidden.hide(hidden.SCOPE_MISSING, [("New", "Album", "2020")])  # load -> save

    corrupt = p.with_name(p.name + ".corrupt")
    assert corrupt.exists(), "corrupt store must be kept aside, not silently wiped"
    assert "THIS IS BROKEN" in corrupt.read_text(encoding="utf-8")
    # the new hide still persisted, to a fresh valid file
    saved = hidden.load()
    assert hidden.album_fingerprint("New", "Album") in saved[hidden.SCOPE_MISSING]
    # Invalid UTF-8 is kept aside too, without replacing the first copy.
    p.write_bytes(b'{"missing": {}}\xff')
    hidden.load()
    assert p.with_name(p.name + ".corrupt.2").read_bytes() == b'{"missing": {}}\xff'
