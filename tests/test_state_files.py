"""A corrupt state file must survive the run that found it.

Every store under DATA_DIR is read as load-modify-save, so a loader that
answers "empty" for a file it couldn't parse lets the next save flatten it.
These cover the two that cost the most: the lyrics state (wiped by ordinary
CLI startup housekeeping) and the upgrade caps (whose loss re-offers hi-res
downloads for albums deliberately kept at CD rate).
"""
from qobuz_librarian.integrations import lyric_fetch
from qobuz_librarian.quality import decision

TRUNCATED = ('{"/music/A/B/01.flac": {"mtime": 1.0, "size": 2, '
             '"status": "synced"},\n {"/music/A/B/02.flac": ')


def test_corrupt_lyrics_state_survives_startup_housekeeping(tmp_path):
    path = tmp_path / ".lyric_fetch_state.json"
    path.write_text(TRUNCATED, encoding="utf-8")

    # What cli.py runs on every launch: read the state, prune, write it back.
    lyric_fetch.update_state(lambda state: None, path)

    kept = path.with_name(path.name + ".corrupt")
    assert kept.exists(), "unreadable lyrics state must be kept, not replaced"
    assert kept.read_text(encoding="utf-8") == TRUNCATED
    assert path.read_text(encoding="utf-8") == "{}"


def test_corrupt_upgrade_caps_survive_a_new_cap(tmp_path, monkeypatch):
    path = tmp_path / "capped.json"
    path.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(decision.cfg, "CAPPED_FILE", path)
    monkeypatch.setattr(decision.cfg, "MUSIC_ROOT", tmp_path)
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    decision.mark_local_album_capped(album_dir)

    kept = path.with_name(path.name + ".corrupt")
    assert kept.read_text(encoding="utf-8") == "not json at all"
    assert decision.is_local_album_capped(album_dir, decision.load_capped())
