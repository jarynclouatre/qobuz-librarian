"""The dismissed-store key must tell distinct albums apart.

The loose decoration strip collapsed 'Alone' with 'Alone (Again)' - dismissing
one buried the other, and a kept album could vanish under a dismissed
sibling's fingerprint. The strict key keeps identity-bearing parentheses while
still folding real edition decorations.
"""
from qobuz_librarian.library import hidden


def test_distinct_albums_get_distinct_fingerprints():
    pairs = [
        ("Alone", "Alone (Again)"),
        ("Rancid", "Rancid (5)"),
        ("The Asylum Albums (1972-1975)", "The Asylum Albums (1976-1980)"),
    ]
    for a, b in pairs:
        fa = hidden.album_fingerprint("Artist", a)
        fb = hidden.album_fingerprint("Artist", b)
        assert fa and fb and fa != fb, (a, b)
    # ...while an edition still folds onto its album, whatever the case.
    assert (hidden.album_fingerprint("Artist", "Revolver")
            == hidden.album_fingerprint("Artist", "Revolver (2009 Remaster)")
            == hidden.album_fingerprint("artist", "REVOLVER"))
    # Nothing left to compare on, so it can never be hidden.
    assert hidden.album_fingerprint("", "Kid A") is None
    assert hidden.album_fingerprint("Radiohead", "") is None


def test_loose_keyed_store_rekeys_and_splits_on_load(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg

    store_path = tmp_path / "hidden.json"
    monkeypatch.setattr(cfg, "HIDDEN_FILE", store_path)
    # A store written by the old key: one entry whose single loose fingerprint
    # covers two genuinely different albums.
    old_key = "artist|alone"
    store_path.write_text(
        '{"missing": {"%s": {"artist": "Artist", "title": "Alone", '
        '"ts": "2026-08-01", "rows": ['
        '{"title": "Alone", "year": "1998", "ts": "2026-08-01"},'
        '{"title": "Alone (Again)", "year": "2021", "ts": "2026-08-01"}'
        ']}}}' % old_key,
        encoding="utf-8")

    bucket = hidden.load()["missing"]
    fp_alone = hidden.album_fingerprint("Artist", "Alone")
    fp_again = hidden.album_fingerprint("Artist", "Alone (Again)")
    assert set(bucket) == {fp_alone, fp_again}
    assert [r["title"] for r in bucket[fp_alone]["rows"]] == ["Alone"]
    assert [r["title"] for r in bucket[fp_again]["rows"]] == ["Alone (Again)"]
    # Bringing one back leaves the other dismissed - the burial this key
    # change exists to end.
    hidden.restore_albums("missing", [fp_again])
    after = hidden.load()["missing"]
    assert fp_alone in after and fp_again not in after
