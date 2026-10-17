"""Library migration: placement correctness and the copy-safety guarantees."""
import csv
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qobuz_librarian.library import migrate as m


def _meta(**kw):
    base = {
        "albumartist": "Artist", "album": "Album", "title": "Song",
        "track": 1, "disc": 1, "disctotal": 0, "year": 2017,
        "compilation": False, "ext": ".flac",
    }
    base.update(kw)
    return base


# ── tag normalization ────────────────────────────────────────────────────────

def test_normalize_tags_parses_slashed_track_and_disc_and_year():
    meta = m.normalize_tags(
        {"albumartist": ["A"], "album": ["B"], "title": ["T"],
         "tracknumber": ["3/12"], "discnumber": ["2/2"], "date": ["2008-05-01"]},
        stem="03 T", ext=".flac")
    assert meta["track"] == 3
    assert meta["disc"] == 2
    assert meta["disctotal"] == 2
    assert meta["year"] == 2008
    meta = m.normalize_tags(
        {"albumartist": ["A"], "album": ["B"], "title": ["T"], "date": ["1900-01-01"]},
        stem="T", ext=".flac")
    assert m.album_components(meta) == ("A", "B")


# ── placement ─────────────────────────────────────────────────────────────────

def test_plan_places_tracks_in_the_beets_layout_and_never_guesses(tmp_path):
    """Tracks land at Artist/Album (Year)/NN - Title, tracks of one album
    tagged with different years stay in one folder, and a track missing its
    artist or album is left unplaceable rather than guessed."""
    source = tmp_path / "layout"
    source.mkdir()
    track = source / "x.flac"
    track.write_bytes(b"audio")
    plan = m.build_plan(
        [(track, _meta(track=4, title="Hey"), "tags")],
        tmp_path / "dest",
    )
    assert plan.placed[0].dest_rel == Path("Artist/Album (2017)/04 - Hey.flac")

    source = tmp_path / "years" / "Album"
    source.mkdir(parents=True)
    items = []
    for number, year in enumerate((1999, 2005, 2005), 1):
        track = source / f"{number:02d}.flac"
        track.write_bytes(b"audio %d" % number)
        items.append((track, _meta(track=number, title=f"Song {number}",
                                   year=year), "tags"))
    plan = m.build_plan(items, tmp_path / "dest")
    assert {e.dest_rel.parent for e in plan.placed} == {
        Path("Artist/Album (2005)")}

    source = tmp_path / "untagged"
    source.mkdir()
    first = source / "a.flac"
    second = source / "b.flac"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    plan = m.build_plan([
        (first, _meta(albumartist=""), "tags"),
        (second, None, ""),
    ], tmp_path / "dest")
    assert len(plan.unplaceable) == 2
    assert not plan.placed


# ── AcoustID match selection ──────────────────────────────────────────────────

def test_acoustid_places_a_track_only_on_a_confident_unambiguous_match():
    """Low-confidence and ambiguous matches are rejected. A confident lookup
    prefers the Album release group over a Single and takes its earliest
    year, which makes the track placeable."""
    assert m.choose_acoustid_match([{"score": 0.5, "artist": "A"}]) is None
    assert m.choose_acoustid_match([
        {"score": 0.95, "artist": "Oasis"},
        {"score": 0.93, "artist": "Blur"},
    ]) is None
    chosen = m.choose_acoustid_match([{"score": 0.97, "artist": "Oasis", "title": "T"}])
    assert chosen["artist"] == "Oasis"

    resp = {"results": [{"score": 0.98, "recordings": [{
        "title": "Kong", "artists": [{"name": "Bonobo"}],
        "releasegroups": [
            {"type": "Single", "title": "Kong", "releases": [{"date": {"year": 2009}}]},
            {"type": "Album", "title": "Black Sands", "artists": [{"name": "Bonobo"}],
             "releases": [{"date": {"year": 2011}}, {"date": {"year": 2010}}]},
        ]}]}]}
    meta = m.identify_from_lookup(resp, 0.9, "stem", ".flac")
    assert meta["album"] == "Black Sands"
    assert meta["year"] == 2010
    assert meta["albumartist"] == "Bonobo"
    assert m.is_placeable(meta)


def test_fingerprinting_keeps_full_duration_and_rejects_disguised_playlists(
        tmp_path, monkeypatch):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe not available")
    formats = subprocess.run(
        ["ffmpeg", "-hide_banner", "-formats"],
        capture_output=True, text=True, check=True,
    )
    if "chromaprint" not in formats.stdout:
        pytest.skip("ffmpeg has no Chromaprint support")
    track = tmp_path / "untagged.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=123",
         "-c:a", "adpcm_ima_wav", str(track)],
        check=True,
    )
    lookups = []

    def lookup(_key, fingerprint, duration, **_kwargs):
        lookups.append((fingerprint, duration))
        return {"results": [{"score": 0.99, "recordings": [{
            "title": "Song", "artists": [{"name": "Artist"}],
            "releasegroups": [{"title": "Album", "type": "Album"}],
        }]}]}

    monkeypatch.setitem(sys.modules, "acoustid", SimpleNamespace(lookup=lookup))
    with track.open("rb") as source:
        result = m.fingerprint_identify(track, descriptor=source.fileno())
    assert result is not None and result["album"] == "Album"
    assert lookups[0][0]
    assert lookups[0][1] == pytest.approx(123, abs=0.1)

    track.write_text('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period></MPD>')

    def decode_unrecognized_file(*_args, **_kwargs):
        pytest.fail("unrecognized audio reached the native decoder")

    monkeypatch.setattr(m.subprocess, "run", decode_unrecognized_file)
    with track.open("rb") as source:
        assert m.fingerprint_identify(track, descriptor=source.fileno()) is None
    assert len(lookups) == 1


def _placed_plan(tmp_path, n=1, *, existing_destination_root=False,
                 existing_destination_parents=False, companion_bytes=0):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    items = []
    for i in range(n):
        p = src_dir / f"track{i}.flac"
        p.write_bytes(b"audio-bytes-%d" % i)
        items.append((p, _meta(title=f"Song {i}", track=i + 1), "tags"))
    if companion_bytes:
        (src_dir / "booklet.pdf").write_bytes(b"x" * companion_bytes)
    destination = tmp_path / "dest"
    if existing_destination_parents:
        (destination / "Artist" / "Album (2017)").mkdir(parents=True)
    elif existing_destination_root:
        destination.mkdir()
    return m.build_plan(items, destination)


def test_copy_keeps_originals_and_in_place_moves_only_after_a_verified_copy(tmp_path):
    """Copy mode leaves the originals and carries their mode, modification
    time and lyrics. In-place mode moves only after a verified copy and names what
    it left behind in each source folder."""
    (tmp_path / "copy").mkdir()
    plan = _placed_plan(tmp_path / "copy")
    src = plan.placed[0].source
    src.chmod(0o640)
    m.os.utime(
        src,
        ns=(1_600_000_000_000_000_000, 1_600_000_010_000_000_000),
    )
    source_times = src.stat()
    lyrics = "[00:01.00]Words kept with the song\n"
    src.with_suffix(".lrc").write_text(lyrics)
    plan = m.build_plan(
        [(src, _meta(title="Song 0", track=1), "tags")],
        plan.dest_root,
    )
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 1 and res.failed == 0
    assert src.exists()
    dst = plan.dest_root / plan.placed[0].dest_rel
    assert dst.stat().st_mode & 0o777 == 0o640
    assert dst.stat().st_mtime_ns == source_times.st_mtime_ns
    assert dst.read_bytes() == src.read_bytes()
    assert dst.with_suffix(".lrc").read_text() == lyrics
    assert src.with_suffix(".lrc").read_text() == lyrics
    assert not dst.with_name(dst.name + ".partial").exists()

    root = tmp_path / "move"
    root.mkdir()
    plan = _placed_plan(root, n=2)
    album = root / "src" / "Album"
    (album / "Scans").mkdir(parents=True)
    (album / "Scans" / "back.jpg").write_bytes(b"scan")
    (album / "readme.txt").write_text("notes")
    items = []
    for entry, folder in zip(plan.placed, (album, root / "src" / "Disc")):
        folder.mkdir(exist_ok=True)
        source = entry.source.rename(folder / entry.source.name)
        source.with_suffix(".lrc").write_text(source.name)
        items.append((source, entry.meta, entry.source_of_truth))
    plan = m.build_plan(items, plan.dest_root)
    res = m.execute_plan(plan, in_place=True)
    assert res.copied == 2 and res.companions == 2
    # The copied lyrics stay in both, but only these never reached the new
    # library.
    assert res.left_in_source == [
        ("Album", ["Scans/", "readme.txt"]), ("Disc", [])]
    for entry in plan.placed:
        assert not entry.source.exists()
        destination = plan.dest_root / entry.dest_rel
        assert destination.exists()
        assert destination.with_suffix(".lrc").read_text() == entry.source.name


def test_execute_never_clobbers_a_destination_or_moves_a_changed_source(
        tmp_path, monkeypatch):
    """A destination that wins the publish race is not overwritten, a source
    replaced after the preview and a destination parent turned into a symlink
    are refused, and an interrupt right after publishing keeps the exact
    source, takes the published copy back and records the run as
    cancelled. A source rewritten after its copy was published is kept with
    its new bytes and the copy taken back, and an interrupt right after the
    original is deleted keeps the copy."""
    def case(name, **kwargs):
        (tmp_path / name).mkdir()
        return _placed_plan(tmp_path / name, **kwargs)

    plan = case("race", existing_destination_parents=True)
    dst = plan.dest_root / plan.placed[0].dest_rel
    rename_noreplace = m._rename_noreplace_at
    raced = False

    def publish_racer(source_fd, source_name, destination_fd, destination_name):
        nonlocal raced
        if source_name.startswith(".qobuz-migrate-copy-") and not raced:
            raced = True
            descriptor = m.os.open(
                destination_name,
                m.os.O_WRONLY | m.os.O_CREAT | m.os.O_EXCL,
                0o600,
                dir_fd=destination_fd,
            )
            m.os.write(descriptor, b"precious")
            m.os.close(descriptor)
        return rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)

    with monkeypatch.context() as patch:
        patch.setattr(m, "_rename_noreplace_at", publish_racer)
        res = m.execute_plan(plan, in_place=False)
    assert res.copied == 0 and res.skipped == 1
    assert dst.read_bytes() == b"precious"                # not clobbered
    assert plan.placed[0].source.exists()

    plan = case("replaced")
    source = plan.placed[0].source
    reviewed = source.with_name("reviewed.flac")
    source.rename(reviewed)
    source.write_bytes(b"unrelated replacement")
    result = m.execute_plan(plan, in_place=True)
    destination = plan.dest_root / plan.placed[0].dest_rel
    assert result.failed == 1
    assert not destination.exists()
    assert source.read_bytes() == b"unrelated replacement"
    assert reviewed.read_bytes().startswith(b"audio-bytes-")

    plan = case("symlinked", existing_destination_root=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (plan.dest_root / "Artist").symlink_to(outside, target_is_directory=True)
    result = m.execute_plan(plan, in_place=False)
    assert result.failed == 1
    assert not list(outside.rglob("*.flac"))
    assert plan.placed[0].source.exists()

    plan = case("interrupted", existing_destination_parents=True)
    destination = plan.dest_root / plan.placed[0].dest_rel

    def interrupt_after_publish(
            source_fd, source_name, destination_fd, destination_name):
        rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)
        if source_name.startswith(".qobuz-migrate-copy-"):
            raise KeyboardInterrupt

    monkeypatch.setattr(m, "_rename_noreplace_at", interrupt_after_publish)

    result = m.execute_plan(plan, in_place=True)

    assert result.cancelled and result.interrupted and result.failed == 0
    assert plan.placed[0].source.read_bytes().startswith(b"audio-bytes-")
    assert not destination.exists()
    assert not list(destination.parent.glob(".qobuz-migrate-copy-*"))

    monkeypatch.setattr(m, "_rename_noreplace_at", rename_noreplace)
    results = m.write_results_manifest(result, plan=plan)
    with results.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    summary = next(row for row in rows if row["record"] == "summary")
    counters = json.loads(summary["reason"])
    assert summary["status"] == "cancelled"
    assert counters["cancelled"] is True
    assert counters["failed"] == 0
    assert counters["pruned"] == 0
    track = next(row for row in rows if row["record"] == "track")
    assert track["status"] == m.INTERRUPTED

    plan = case("rewritten after publishing")
    source = plan.placed[0].source
    retire_source = m._retire_source

    def rewrite_before_delete(*args):
        source.write_bytes(b"edited by a tagger")
        return retire_source(*args)

    with monkeypatch.context() as patch:
        patch.setattr(m, "_retire_source", rewrite_before_delete)
        result = m.execute_plan(plan, in_place=True)
    assert result.failed == 1 and result.lingered == 0
    assert source.read_bytes() == b"edited by a tagger"
    assert not (plan.dest_root / plan.placed[0].dest_rel).exists()

    plan = case("interrupted after the delete")

    def interrupt_after_delete(*args):
        assert retire_source(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(m, "_retire_source", interrupt_after_delete)
    result = m.execute_plan(plan, in_place=True)
    assert result.interrupted
    assert not plan.placed[0].source.exists()
    assert (plan.dest_root / plan.placed[0].dest_rel).read_bytes() \
        == b"audio-bytes-0"


# ── web flow (scan → review candidates → execute copy) ─────────────────────────

def _sealed_web_choice(files, dest):
    items = [
        (path, _meta(title=path.stem, track=index), "tags")
        for index, path in enumerate(files, 1)
    ]
    plan = m.build_plan(items, dest)
    m.write_manifest(plan)
    return [{"payload": {
        "entries": [
            (str(entry.source), str(entry.dest_rel), entry.source_receipt,
             entry.destination_path_receipt)
            for entry in plan.placed
        ],
        "source_root": str(plan.source_root),
        "source_root_receipt": plan.source_root_receipt,
        "dest_root_receipt": plan.dest_root_receipt,
        "destination_name_semantics": plan.destination_name_semantics,
        "companion_receipts": plan.companion_receipts,
    }}]

def test_web_migration_copies_the_review_and_holds_a_short_move_for_consent(
        tmp_path, monkeypatch):
    """The reviewed tracks are copied with the originals kept. An in-place
    move into a destination known to be short on space is refused before any
    file is touched, since running out mid-move would scatter the library,
    unless the deliberate low-space override is passed, and a scan of such a
    move asks for that acknowledgement in its review."""
    from qobuz_librarian.library import migrate as engine
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    src = tmp_path / "copy" / "src"
    src.mkdir(parents=True)
    f1, f2 = src / "x.flac", src / "y.flac"
    f1.write_bytes(b"one")
    f2.write_bytes(b"two")
    dest = tmp_path / "copy" / "dest"
    chosen = _sealed_web_choice((f1, f2), dest)
    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=False)
    copied = sorted((dest / "Artist/Album (2017)").glob("*.flac"))
    assert [path.read_bytes() for path in copied] == [b"one", b"two"]
    assert f1.exists() and f2.exists()             # copy mode: originals intact
    assert job.status is not jm.JobStatus.FAILED and not job.error
    assert list(dest.glob("migration-results-*.csv"))

    src = tmp_path / "move" / "src"
    src.mkdir(parents=True)
    f1 = src / "x.flac"
    f1.write_bytes(b"one")
    dest = tmp_path / "move" / "dest"
    chosen = _sealed_web_choice((f1,), dest)
    destination = dest / chosen[0]["payload"]["entries"][0][1]
    monkeypatch.setattr(
        engine, "space_estimate",
        lambda plan, in_place, resume_entries=None: (10_000, 10),
    )

    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=True, src=src)
    assert job.status is jm.JobStatus.FAILED and job.error
    assert not destination.exists()
    assert f1.exists()                                   # nothing was moved

    job2 = jm.Job(title="mig2")
    flows.execute_migration(job2, chosen, str(dest), in_place=True, src=src,
                            allow_low_space=True)
    assert not job2.error
    assert destination.exists()

    (tmp_path / "scan").mkdir()
    plan = _placed_plan(tmp_path / "scan")
    monkeypatch.setattr(engine, "collect_items", lambda *_args, **_kwargs: ["item"])
    monkeypatch.setattr(engine, "build_plan", lambda _items, _dest: plan)
    monkeypatch.setattr(engine, "verified_resume_entries", lambda *_args, **_kwargs: [])
    job = jm.Job(title="migration")
    job.execute_args = {"allow_low_space": False}

    flows.scan_migration(
        job, plan.source_root, plan.dest_root,
        use_acoustid=False, in_place=True,
    )

    assert job.execute_args["requires_low_space_override"] is True


def test_terminal_migration_gates_a_short_move_and_leaves_a_dry_run_untouched(
        tmp_path, monkeypatch):
    """Short on space, an unattended (--yes) run refuses outright, since a
    partial in-place move scatters the library, and an interactive run needs
    a typed override rather than a stray "y". A dry run, twice over, leaves
    the destination as it was."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from qobuz_librarian.modes import migrate as migrate_mode

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    track = source / "track.flac"
    track.write_bytes(b"audio-bytes")
    existing = destination / "keep.txt"
    existing.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(migrate_mode, "HAVE_MUTAGEN", True)
    monkeypatch.setattr(
        migrate_mode.engine,
        "collect_items",
        lambda *_args, **_kwargs: [(track, _meta(title="Song 0", track=1), "tags")],
    )
    args = SimpleNamespace(
        migrate_src=str(source),
        migrate_dest=str(destination),
        dry_run=True,
        yes=False,
        verbose=False,
        in_place=False,
        acoustid=False,
    )

    assert migrate_mode.run_migrate_mode(args) == 0
    assert migrate_mode.run_migrate_mode(args) == 0
    assert list(destination.iterdir()) == [existing]
    assert existing.read_text(encoding="utf-8") == "unchanged"

    src = tmp_path / "short" / "src"
    dest = tmp_path / "short" / "dest"
    src.mkdir(parents=True)
    dest.mkdir()
    plan = SimpleNamespace(
        placed=[SimpleNamespace(source=src / "a.flac",
                                dest_rel=Path("Artist/Album/a.flac"))],
        unplaceable=[], collisions=[],
        summary=lambda: {"place": 1, "unplaceable": 0, "collision": 0})

    monkeypatch.setattr(migrate_mode, "_resolve_paths", lambda args: (src, dest))
    monkeypatch.setattr(migrate_mode.engine, "collect_items", lambda *a, **k: [object()])
    monkeypatch.setattr(migrate_mode.engine, "build_plan", lambda items, d: plan)
    monkeypatch.setattr(
        migrate_mode.engine,
        "write_manifest",
        lambda *a, **k: dest / "preview.csv",
    )
    executed = []

    def _fake_execute(*a, **k):
        executed.append(1)
        return SimpleNamespace(copied=1, skipped=0, lingered=0, failed=0,
                               cancelled=False, interrupted=False,
                               failures=[], outcomes=[],
                               companion_outcomes=[])
    monkeypatch.setattr(migrate_mode.engine, "execute_plan", _fake_execute)
    monkeypatch.setattr(
        migrate_mode.engine,
        "write_results_manifest",
        lambda *a, **k: dest / "results.csv",
    )

    def _args(**kw):
        base = dict(dry_run=False, yes=False, verbose=False, in_place=True, acoustid=False)
        base.update(kw)
        return SimpleNamespace(**base)

    # Short on space + unattended → refuse, no partial move.
    monkeypatch.setattr(
        migrate_mode.engine, "space_estimate",
        lambda p, in_place, resume_entries=None: (100, 10),
    )
    migrate_mode.run_migrate_mode(_args(yes=True))
    assert executed == []

    # Short + interactive: a casual decline cancels…
    with patch("builtins.input", side_effect=["no"]):
        migrate_mode.run_migrate_mode(_args())
    assert executed == []
    # …only a typed "yes" overrides.
    with patch("builtins.input", side_effect=["yes"]):
        migrate_mode.run_migrate_mode(_args())
    assert executed == [1]

    # Enough space → the normal confirm path still runs.
    executed.clear()
    monkeypatch.setattr(
        migrate_mode.engine, "space_estimate",
        lambda p, in_place, resume_entries=None: (10, 100),
    )
    with patch("builtins.input", side_effect=["y"]):
        migrate_mode.run_migrate_mode(_args())
    assert executed == [1]


# ── statx directory-incarnation proof ────────────────────────────────────────

def _statx_result(mask):
    """A well-formed directory statx result carrying exactly ``mask``."""
    value = m._Statx()
    value.mask = mask
    value.mode = stat.S_IFDIR | 0o755
    value.ino = 4242
    value.dev_minor = 64
    value.btime.tv_sec = 1780678551
    value.btime.tv_nsec = 803587439
    value.mnt_id = 9583
    return value


def test_directory_identity_needs_a_mount_id_but_not_atime(monkeypatch):
    """ZFS with atime=off reports every field the proof reads but clears
    STATX_ATIME, and the incarnation proof must still succeed; without a
    mount id it must not."""
    mask = (m._STATX_BASIC_STATS & ~0x0020) | m._STATX_BTIME | m._STATX_MNT_ID
    monkeypatch.setattr(m, "_statx_probe", lambda *a: _statx_result(mask))
    identity = m._statx_directory_identity(3, b"", 0)
    assert identity[2] == 4242          # inode
    assert identity[4] == 1780678551    # birth time
    assert identity[6] == 9583          # mount id

    mask = m._STATX_BASIC_STATS | m._STATX_BTIME  # no STATX_MNT_ID
    monkeypatch.setattr(m, "_statx_probe", lambda *a: _statx_result(mask))
    with pytest.raises(OSError):
        m._statx_directory_identity(3, b"", 0)


