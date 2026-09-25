"""Web path for single-track grabs: the Get-track download contract and its
Undo."""

import pytest
from test_web import _remove_job, _wait_for, client  # noqa: F401 (fixture)

from qobuz_librarian.library import hidden
from qobuz_librarian.web import jobs as jm


def _owned_path(root, path):
    """Filesystem identity record written after a single-track import."""
    from qobuz_librarian.web.owned_paths import _bind_owned_path

    owned = _bind_owned_path(root, path)
    assert owned is not None
    return owned


def _ownership_manifest(root, path, *, created=()):
    def identity(target):
        st = target.stat()
        return {
            "device": st.st_dev,
            "inode": st.st_ino,
            "size": st.st_size,
            "modified_ns": st.st_mtime_ns,
            "changed_ns": st.st_ctime_ns,
        }

    return {
        "version": 1,
        "sealed": True,
        "root": str(root),
        "root_identity": identity(root),
        "items": [{
            "relative": path.relative_to(root).as_posix(),
            "file": identity(path),
            "created_directories": [
                {"relative": directory.relative_to(root).as_posix(),
                 **identity(directory)}
                for directory in created
            ],
            "companions": [],
        }],
    }


@pytest.fixture
def fresh_singles(tmp_path, monkeypatch):
    from qobuz_librarian import config as cfg
    from qobuz_librarian.web import job_persistence

    monkeypatch.setattr(cfg, "HIDDEN_FILE", tmp_path / "hidden.json")
    monkeypatch.setattr(job_persistence, "persist", lambda _job: True)


def test_get_track_marks_the_single_with_the_gap_toggle_off(
        client, monkeypatch, fresh_singles):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.web import runtime

    monkeypatch.setattr(runtime, "_get_token", lambda: "tok")
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "alb1", "title": "Girl With No Face", "year": 2024,
        "artist": {"name": "Allie X"},
        "tracks": {"items": [
            {"id": "trk7", "title": "Black Eye", "track_number": 3},
            {"id": "trk8", "title": "Galina", "track_number": 4},
            {"id": "trk9", "title": "Off With Her Tits", "track_number": 5}]}})
    # own none of it, so the grabbed track leaves the album partial -> a single
    monkeypatch.setattr(cat_mod, "find_existing_tracks", lambda *a, **k: ([], None))

    def fake_exec(queue, *a, **k):
        queue[0]["n_ok"] = 1
        queue[0]["imported"] = True
        queue[0]["n_fail"] = 0
    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)

    jm.start_worker()
    monkeypatch.setattr(app_mod.cfg, "SUPPRESS_SINGLE_TRACK_GAPS", False, raising=False)
    r = client.post("/download", data={"album_id": "alb1", "track_id": "trk7"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    jobs = [j for j in list(jm.registry._jobs.values())
            if getattr(j, "album_id", None) == "alb1"]
    assert len(jobs) == 1
    job = jobs[0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        # Upgrade reads the mark; gap scans only do with the toggle on.
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is True
    finally:
        _remove_job(job)


def test_undo_removes_the_grabbed_track_and_clears_the_mark(client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.scanner as scanner_mod
    import qobuz_librarian.web.flows as flows_mod
    from qobuz_librarian.web import runtime

    d = tmp_path / "Allie X" / "Girl With No Face (2024)"
    d.mkdir(parents=True)
    f = d / "03 - Black Eye.flac"
    f.write_bytes(b"flac")
    cover = d / "cover.jpg"
    cover.write_bytes(b"older artwork")
    hidden.mark_single("Allie X", "Girl With No Face", "2024", "alb1")
    refresh_calls = []

    job = jm.Job(title="Black Eye", artist="Allie X", album_id="alb1")
    job.status = jm.JobStatus.DONE
    job.single = {"album_id": "alb1", "track_id": "trk7", "dir": str(d),
                  "isrc": "ISRC1", "track_no": 3, "title": "Black Eye",
                  "artist": "Allie X", "album": "Girl With No Face",
                  "marked": True, "new_folder": True,
                  "owned_path": _owned_path(d, f)}
    jm.registry.add(job)
    monkeypatch.setattr(runtime, "_get_optional_token", lambda: "tok")
    monkeypatch.setattr(
        flows_mod,
        "_refresh_after_local_album_change",
        lambda *args, **kwargs: refresh_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(scanner_mod, "read_album_dir",
                        lambda _d: [{"path": str(f), "isrc": "ISRC1", "track": 3}])
    monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
    try:
        r = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert r.status_code in (200, 303)
        assert not f.exists()  # the grabbed track is gone
        assert cover.read_bytes() == b"older artwork"
        assert d.is_dir()
        assert hidden.is_single("Allie X", "Girl With No Face", hidden.load()) is False
        assert job.single.get("removed") is True
        assert len(refresh_calls) == 1
    finally:
        _remove_job(job)


def test_undo_refuses_a_replacement_when_the_inode_is_reused(
        client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.integrations.beets as beets_mod

    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    track = d / "01 - Track.flac"
    track.write_bytes(b"downloaded copy")
    owned = _owned_path(d, track)

    track.unlink()
    replacement = b"my curated replacement audio"
    track.write_bytes(replacement)
    replacement_stat = track.stat()
    # Model the normal delete-then-create case where the filesystem recycles
    # the old inode.
    owned["file"]["device"] = replacement_stat.st_dev
    owned["file"]["inode"] = replacement_stat.st_ino

    job = jm.Job(title="Track", artist="Artist", album_id="album")
    job.status = jm.JobStatus.DONE
    job.single = {
        "dir": str(d), "track_id": "track", "title": "Track",
        "artist": "Artist", "album": "Album", "marked": False,
        "owned_path": owned,
    }
    jm.registry.add(job)
    forgotten = []
    monkeypatch.setattr(
        beets_mod, "forget_beets_entries",
        lambda paths: forgotten.extend(paths),
    )
    try:
        response = client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert response.status_code in (200, 303)
        assert track.read_bytes() == replacement
        assert not job.single.get("removed")
        assert forgotten == []
    finally:
        _remove_job(job)


def test_undo_no_isrc_removes_the_grabbed_disc_not_a_same_numbered_twin(
        client, monkeypatch, fresh_singles, tmp_path):
    import qobuz_librarian.api.search as search_mod
    import qobuz_librarian.integrations.beets as beets_mod
    import qobuz_librarian.library.catalog as cat_mod
    import qobuz_librarian.queue.executor as ex_mod
    import qobuz_librarian.web.app as app_mod
    from qobuz_librarian.web import runtime

    music_root = tmp_path / "music"
    d = music_root / "By Genre" / "Classical" / "Artist" / "Box Set (2020)"
    cd1 = d / "Disc 1"
    cd2 = d / "Disc 2"
    cd1.mkdir(parents=True)
    cd1_twin = cd1 / "03 - Disc One Three.flac"
    cd2_grabbed = cd2 / "03 - Disc Two Three.flac"
    cd1_twin.write_bytes(b"cd1")

    monkeypatch.setattr(runtime, "_get_token", lambda: "tok")
    monkeypatch.setattr(app_mod.cfg, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(search_mod, "get_album", lambda _id, _tok: {
        "id": "albx", "title": "Box Set", "year": 2020,
        "artist": {"name": "Artist"},
        "tracks": {"items": [
            {"id": "cd1t3", "title": "Disc One Three",
             "track_number": 3, "media_number": 1},
            {"id": "cd2t3", "title": "Disc Two Three",
             "track_number": 3, "media_number": 2}]}})
    existing = [{
        "path": str(cd1_twin), "title": "Disc One Three", "isrc": "",
        "tracknumber": 3, "discnumber": 1,
    }]
    monkeypatch.setattr(
        cat_mod, "find_existing_tracks", lambda *a, **k: (existing, d))

    def fake_exec(queue, *a, **k):
        cd2.mkdir()
        cd2_grabbed.write_bytes(b"cd2")
        queue[0]["n_ok"] = 1
        queue[0]["imported"] = True
        queue[0]["n_fail"] = 0
        queue[0]["_resolved_post_dir"] = str(d)
        queue[0]["_import_ownership"] = _ownership_manifest(
            music_root,
            cd2_grabbed,
            created=[cd2],
        )
    monkeypatch.setattr(ex_mod, "_execute_download_queue", fake_exec)

    jm.start_worker()
    r = client.post("/download", data={"album_id": "albx", "track_id": "cd2t3"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    job = [j for j in list(jm.registry._jobs.values())
           if getattr(j, "album_id", None) == "albx"][0]
    try:
        assert _wait_for(lambda: job.status in (jm.JobStatus.DONE, jm.JobStatus.FAILED))
        assert job.status == jm.JobStatus.DONE
        assert job.single.get("disc_no") == 2
        assert job.single.get("owned_path")

        sibling_album = d.parent / "Other Album"
        sibling_album.mkdir()
        monkeypatch.setattr(beets_mod, "forget_beets_entries", lambda paths: len(paths))
        client.post(f"/jobs/{job.id}/undo", follow_redirects=False)
        assert not cd2_grabbed.exists()
        assert not cd2.exists()
        assert d.is_dir()
        assert sibling_album.is_dir()
        assert cd1.is_dir()
        assert cd1_twin.exists()
    finally:
        _remove_job(job)
