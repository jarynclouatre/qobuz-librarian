"""Library migration: placement correctness and the copy-safety guarantees."""
import csv
import json
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


# ── destination path ──────────────────────────────────────────────────────────

def test_destination_matches_beets_layout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    track = source / "x.flac"
    track.write_bytes(b"audio")
    plan = m.build_plan(
        [(track, _meta(track=4, title="Hey"), "tags")],
        tmp_path / "dest",
    )
    assert plan.placed[0].dest_rel == Path("Artist/Album (2017)/04 - Hey.flac")


# ── classification ─────────────────────────────────────────────────────────────

def test_missing_artist_or_album_is_unplaceable_not_guessed(tmp_path):
    source = tmp_path / "source"
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

def test_acoustid_rejects_low_confidence_and_ambiguous_matches():
    assert m.choose_acoustid_match([{"score": 0.5, "artist": "A"}]) is None
    assert m.choose_acoustid_match([
        {"score": 0.95, "artist": "Oasis"},
        {"score": 0.93, "artist": "Blur"},
    ]) is None
    chosen = m.choose_acoustid_match([{"score": 0.97, "artist": "Oasis", "title": "T"}])
    assert chosen["artist"] == "Oasis"



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


def test_copy_mode_leaves_originals_untouched(tmp_path):
    plan = _placed_plan(tmp_path)
    src = plan.placed[0].source
    src.chmod(0o640)
    m.os.setxattr(src, "user.qobuz-migration-test", b"preserved")
    m.os.utime(
        src,
        ns=(1_600_000_000_000_000_000, 1_600_000_010_000_000_000),
    )
    source_times = src.stat()
    plan = m.build_plan(
        [(src, _meta(title="Song 0", track=1), "tags")],
        plan.dest_root,
    )
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 1 and res.failed == 0
    assert src.exists()                                   # original sacred
    dst = plan.dest_root / plan.placed[0].dest_rel
    assert dst.stat().st_mode & 0o777 == 0o640
    assert m.os.getxattr(dst, "user.qobuz-migration-test") == b"preserved"
    assert dst.stat().st_atime_ns == source_times.st_atime_ns
    assert dst.stat().st_mtime_ns == source_times.st_mtime_ns
    assert dst.read_bytes() == src.read_bytes()
    assert not dst.with_name(dst.name + ".partial").exists()




def test_in_place_mode_moves_only_after_verified_copy(tmp_path):
    plan = _placed_plan(tmp_path)
    src = plan.placed[0].source
    res = m.execute_plan(plan, in_place=True)
    assert res.copied == 1
    assert not src.exists()                               # moved
    assert (plan.dest_root / plan.placed[0].dest_rel).exists()


def test_execute_never_overwrites_a_destination_that_wins_publish_race(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
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

    monkeypatch.setattr(m, "_rename_noreplace_at", publish_racer)
    res = m.execute_plan(plan, in_place=False)
    assert res.copied == 0 and res.skipped == 1
    assert dst.read_bytes() == b"precious"                # not clobbered
    assert plan.placed[0].source.exists()


def test_execute_refuses_a_source_replacement_after_preview(tmp_path):
    plan = _placed_plan(tmp_path)
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


def test_execute_refuses_a_destination_parent_symlink_after_preview(tmp_path):
    plan = _placed_plan(tmp_path, existing_destination_root=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (plan.dest_root / "Artist").symlink_to(outside, target_is_directory=True)

    result = m.execute_plan(plan, in_place=False)

    assert result.failed == 1
    assert not list(outside.rglob("*.flac"))
    assert plan.placed[0].source.exists()


def test_publish_interrupt_keeps_the_exact_source_and_reconciles_destination(
        tmp_path, monkeypatch):
    plan = _placed_plan(tmp_path, existing_destination_parents=True)
    destination = plan.dest_root / plan.placed[0].dest_rel
    rename_noreplace = m._rename_noreplace_at

    def interrupt_after_publish(
            source_fd, source_name, destination_fd, destination_name):
        rename_noreplace(
            source_fd, source_name, destination_fd, destination_name)
        if source_name.startswith(".qobuz-migrate-copy-"):
            raise KeyboardInterrupt

    monkeypatch.setattr(m, "_rename_noreplace_at", interrupt_after_publish)

    result = m.execute_plan(plan, in_place=True)

    assert result.cancelled and result.failed == 1
    assert plan.placed[0].source.read_bytes().startswith(b"audio-bytes-")
    assert not destination.exists()
    assert result.recoveries == []
    assert not list(destination.parent.glob(".qobuz-migrate-copy-*"))

    monkeypatch.setattr(m, "_rename_noreplace_at", rename_noreplace)
    artifact = m.write_results_manifest(result, plan=plan)
    with Path(artifact["path"]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    summary = next(row for row in rows if row["record"] == "summary")
    counters = json.loads(summary["reason"])
    assert summary["status"] == "cancelled"
    assert counters["cancelled"] is True
    assert counters["failed"] == 1
    assert counters["pruned"] == 0


# ── web flow (scan → review candidates → execute copy) ─────────────────────────

def _sealed_web_choice(files, dest):
    items = [
        (path, _meta(title=path.stem, track=index), "tags")
        for index, path in enumerate(files, 1)
    ]
    plan = m.build_plan(items, dest)
    manifest_artifact = m.write_manifest(plan)
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
        "manifest_artifact": manifest_artifact,
        "companion_receipts": plan.companion_receipts,
    }}]

def test_execute_migration_copies_selected_and_keeps_originals(tmp_path):
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    src = tmp_path / "src"
    src.mkdir()
    f1, f2 = src / "x.flac", src / "y.flac"
    f1.write_bytes(b"one")
    f2.write_bytes(b"two")
    dest = tmp_path / "dest"
    chosen = _sealed_web_choice((f1, f2), dest)
    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=False)
    copied = sorted((dest / "Artist/Album (2017)").glob("*.flac"))
    assert [path.read_bytes() for path in copied] == [b"one", b"two"]
    assert f1.exists() and f2.exists()             # copy mode: originals intact
    assert "2 files copied" in job.summary
    assert list(dest.glob("migration-results-*.csv"))




def test_execute_migration_blocks_low_space_in_place_move(tmp_path, monkeypatch):
    # An in-place move into a destination that's known to be short on space must
    # be refused before any file is touched; running out mid-move would scatter
    # the library unless the user passes the deliberate low-space override.
    from qobuz_librarian.library import migrate as engine
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    src = tmp_path / "src"
    src.mkdir()
    f1 = src / "x.flac"
    f1.write_bytes(b"one")
    dest = tmp_path / "dest"
    chosen = _sealed_web_choice((f1,), dest)
    destination = dest / chosen[0]["payload"]["entries"][0][1]
    monkeypatch.setattr(
        engine, "space_estimate",
        lambda plan, in_place, resume_entries=None: (10_000, 10),
    )

    job = jm.Job(title="mig")
    flows.execute_migration(job, chosen, str(dest), in_place=True, src=src)
    assert job.error and "free space" in job.error.lower()
    assert not destination.exists()
    assert f1.exists()                                   # nothing was moved

    # The explicit override lets the same short move through.
    job2 = jm.Job(title="mig2")
    flows.execute_migration(job2, chosen, str(dest), in_place=True, src=src,
                            allow_low_space=True)
    assert not job2.error
    assert destination.exists()


def test_scan_migration_requires_a_review_ack_for_a_short_in_place_move(
        tmp_path, monkeypatch):
    from qobuz_librarian.library import migrate as engine
    from qobuz_librarian.web import flows
    from qobuz_librarian.web import jobs as jm

    plan = _placed_plan(tmp_path)
    monkeypatch.setattr(engine, "collect_items", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(engine, "build_plan", lambda _items, _dest: plan)
    monkeypatch.setattr(engine, "verified_resume_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        engine,
        "space_estimate",
        lambda _plan, in_place, resume_entries=None: (10_000, 10),
    )
    job = jm.Job(title="migration")
    job.execute_args = {"allow_low_space": False}

    flows.scan_migration(
        job, plan.source_root, plan.dest_root,
        use_acoustid=False, in_place=True,
    )

    assert job.execute_args["requires_low_space_override"] is True
    assert "confirm the low-space risk below" in job.summary


def test_fingerprint_lookup_resolves_album_year_and_is_placeable():
    resp = {"results": [{"score": 0.98, "recordings": [{
        "title": "Kong", "artists": [{"name": "Bonobo"}],
        "releasegroups": [
            {"type": "Single", "title": "Kong", "releases": [{"date": {"year": 2009}}]},
            {"type": "Album", "title": "Black Sands", "artists": [{"name": "Bonobo"}],
             "releases": [{"date": {"year": 2011}}, {"date": {"year": 2010}}]},
        ]}]}]}
    meta = m.identify_from_lookup(resp, 0.9, "stem", ".flac")
    assert meta["album"] == "Black Sands"      # Album type preferred over Single
    assert meta["year"] == 2010                # earliest release year
    assert meta["albumartist"] == "Bonobo"
    assert m.is_placeable(meta)                # the whole point: now placeable


def test_run_migrate_gates_on_insufficient_destination_space(tmp_path, monkeypatch):
    # Short on space: an unattended (--yes) run must refuse outright (a partial
    # in-place move scatters the library), and an interactive run needs a typed
    # override, not the casual confirm that could be answered with a stray "y".
    from types import SimpleNamespace
    from unittest.mock import patch

    from qobuz_librarian.modes import migrate as migrate_mode

    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
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
        lambda *a, **k: {"path": str(dest / "preview.csv")},
    )
    monkeypatch.setattr(
        migrate_mode.engine, "verify_audit_artifact", lambda *a, **k: True)
    executed = []

    def _fake_execute(*a, **k):
        executed.append(1)
        return SimpleNamespace(copied=1, skipped=0, lingered=0, failed=0,
                               cancelled=False, failures=[], outcomes=[],
                               companion_outcomes=[], recoveries=[])
    monkeypatch.setattr(migrate_mode.engine, "execute_plan", _fake_execute)
    monkeypatch.setattr(
        migrate_mode.engine,
        "write_results_manifest",
        lambda *a, **k: {"path": str(dest / "results.csv")},
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


def test_migrate_dry_run_leaves_destination_untouched(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from qobuz_librarian.modes import migrate as migrate_mode

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    existing = destination / "keep.txt"
    existing.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(migrate_mode, "HAVE_MUTAGEN", True)
    monkeypatch.setattr(
        migrate_mode.engine, "collect_items", lambda *_args, **_kwargs: []
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


def test_directory_identity_proves_filesystem_without_atime(monkeypatch):
    # ZFS with atime=off reports every field the proof reads but clears
    # STATX_ATIME; the incarnation proof must still succeed.
    mask = (m._STATX_BASIC_STATS & ~0x0020) | m._STATX_BTIME | m._STATX_MNT_ID
    monkeypatch.setattr(m, "_statx_probe", lambda *a: _statx_result(mask))
    identity = m._statx_directory_identity(3, b"", 0)
    assert identity[2] == 4242          # inode
    assert identity[4] == 1780678551    # birth time
    assert identity[6] == 9583          # mount id




def test_directory_identity_rejects_missing_mount_id(monkeypatch):
    mask = m._STATX_BASIC_STATS | m._STATX_BTIME  # no STATX_MNT_ID
    monkeypatch.setattr(m, "_statx_probe", lambda *a: _statx_result(mask))
    with pytest.raises(OSError):
        m._statx_directory_identity(3, b"", 0)
