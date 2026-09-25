"""Validate Docker defaults and persisted-config reconciliation."""
import os
import re
import subprocess
import tomllib
from pathlib import Path

_DEFAULT_TOML = Path(__file__).resolve().parents[1] / "docker" / "streamrip-default.toml"
_ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"


_PLACEHOLDER_RE = re.compile(r"\{(\w+)(?::[^}]*)?\}")

# Keys streamrip 2.2.0's format() info dict actually provides.
VALID_FOLDER_KEYS = {"albumartist", "albumcomposer", "bit_depth", "container",
                     "id", "sampling_rate", "title", "year"}
VALID_TRACK_KEYS = {"albumartist", "albumcomposer", "artist", "composer",
                    "explicit", "id", "title", "tracknumber"}


def test_streamrip_default_toml_uses_valid_placeholders_and_flags():
    cfg = tomllib.load(open(_DEFAULT_TOML, "rb"))
    folder_keys = {m.group(1) for m in _PLACEHOLDER_RE.finditer(cfg["filepaths"]["folder_format"])}
    track_keys = {m.group(1) for m in _PLACEHOLDER_RE.finditer(cfg["filepaths"]["track_format"])}
    assert not (folder_keys - VALID_FOLDER_KEYS), f"folder uses unknown keys: {folder_keys - VALID_FOLDER_KEYS!r}"
    assert not (track_keys - VALID_TRACK_KEYS), f"track uses unknown keys: {track_keys - VALID_TRACK_KEYS!r}"
    # streamrip's folder formatter has no {album} key.
    fmt = cfg["filepaths"]["folder_format"]
    assert "{album}" not in fmt and "{album:" not in fmt
    # downloads.db dedupe is redundant with our own compute_missing logic;
    # leaving it on makes a re-download of a manually-removed track silently skip.
    assert cfg["database"]["downloads_enabled"] is False
    # Without this, gap-fill walks collapse multi-album fills into one folder.
    assert cfg["filepaths"]["add_singles_to_folder"] is True
    # Booklets aren't imported anywhere, so fetching them just clutters staging.
    assert cfg["qobuz"]["download_booklets"] is False


def _run_entrypoint_head(tmp_path, env_extra, *, capture=False):
    """Run the entrypoint up to (not including) the dispatch case."""
    head, _, _ = _ENTRYPOINT.read_text().partition("# ── Dispatch")
    env = {**os.environ, **env_extra}
    kwargs = dict(env=env, check=not capture)
    if capture:
        kwargs.update(capture_output=True, text=True)
    else:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return subprocess.run(["bash", "-c", head + "\nexit 0\n"], **kwargs)


def _make_config(tmp_path, streamrip_toml: str):
    """Set up a /config layout the entrypoint expects, with the given streamrip toml."""
    cfg = tmp_path / "config"
    (cfg / "streamrip").mkdir(parents=True)
    (cfg / "beets").mkdir(parents=True)
    (cfg / "beets" / "config.yaml").write_text("# placeholder\n")
    (cfg / "streamrip" / "config.toml").write_text(streamrip_toml)
    return cfg


def test_entrypoint_normalises_a_stale_config_volume(tmp_path):
    # Older configs with downloads_enabled=true / add_singles_to_folder=false
    # must be flipped to match the current librarian invariants on every start.
    cfg = _make_config(tmp_path,
        "[qobuz]\n"
        "download_booklets = true\n"
        "[database]\n"
        "downloads_enabled = true\n"
        "failed_downloads_enabled = true\n"
        "[filepaths]\n"
        "add_singles_to_folder = false\n"
        'folder_format = "{albumartist}/{title} ({year})"\n'
    )
    r = _run_entrypoint_head(tmp_path, {"CONFIG_DIR": str(cfg)}, capture=True)
    assert r.returncode == 0
    # With no PUID/PGID the app still drops to 1000:1000, not root.
    assert "1000:1000" in r.stdout

    out = (cfg / "streamrip" / "config.toml").read_text()
    assert "downloads_enabled = false" in out and "\ndownloads_enabled = true" not in out
    assert "add_singles_to_folder = true" in out and "add_singles_to_folder = false" not in out
    assert "download_booklets = false" in out and "download_booklets = true" not in out
    # Unrelated keys are left alone.
    assert "failed_downloads_enabled = true" in out


def test_entrypoint_ownership_repair_does_not_follow_config_symlinks(tmp_path):
    cfg = _make_config(tmp_path, "[database]\n")
    target = tmp_path / "library-track.flac"
    target.write_bytes(b"not really audio")
    link = cfg / "streamrip" / "linked-track.flac"
    link.symlink_to(target)
    called = tmp_path / "chown-called"
    dereferenced = tmp_path / "chown-dereferenced"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_chown = fake_bin / "chown"
    fake_chown.write_text(
        "#!/bin/sh\n"
        ': > "$CHOWN_CALLED"\n'
        "for argument do\n"
        '    [ "$argument" = "-h" ] && exit 0\n'
        "done\n"
        ': > "$CHOWN_DEREFERENCED"\n'
    )
    fake_chown.chmod(0o755)

    _run_entrypoint_head(
        tmp_path,
        {
            "CONFIG_DIR": str(cfg),
            "PUID": str(os.getuid() + 1),
            "PGID": str(os.getgid() + 1),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CHOWN_CALLED": str(called),
            "CHOWN_DEREFERENCED": str(dereferenced),
        },
    )

    assert called.is_file()
    assert not dereferenced.exists()
