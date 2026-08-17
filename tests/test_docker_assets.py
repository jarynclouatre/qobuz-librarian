"""Validate Docker defaults and persisted-config reconciliation."""
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_DEFAULT_TOML = Path(__file__).resolve().parents[1] / "docker" / "streamrip-default.toml"
_ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
_COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"
_ENV_EXAMPLE = _COMPOSE.parent / ".env.example"
_PLACEHOLDER_RE = re.compile(r"\{(\w+)(?::[^}]*)?\}")

# Keys streamrip 2.2.0's format() info dict actually provides.
VALID_FOLDER_KEYS = {"albumartist", "albumcomposer", "bit_depth", "container",
                     "id", "sampling_rate", "title", "year"}
VALID_TRACK_KEYS = {"albumartist", "albumcomposer", "artist", "composer",
                    "explicit", "id", "title", "tracknumber"}


def test_custom_compose_web_port_reaches_cli_recovery_help():
    import yaml

    compose = yaml.safe_load(_COMPOSE.read_text())
    service = compose["services"]["qobuz-librarian"]
    assert "${WEB_BIND:-0.0.0.0}:${WEB_PORT:-8666}:8666" in service["ports"]

    env = {
        **os.environ,
        "WEB_PORT": "8666",
        "WEB_PUBLIC_PORT": "9443",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from qobuz_librarian import cli; print(cli._help_epilog())",
        ],
        cwd=_COMPOSE.parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "http://<host>:9443/settings" in result.stdout


def test_documented_beets_path_survives_compose_env(tmp_path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI unavailable")
    probe = subprocess.run(
        [docker, "compose", "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode:
        pytest.skip("Docker Compose plugin unavailable")
    example = next(
        line.removeprefix("# ")
        for line in _ENV_EXAMPLE.read_text().splitlines()
        if line.startswith("# BEETS_PATH_DEFAULT=")
    )
    env_file = tmp_path / "beets.env"
    env_file.write_text(example + "\n", encoding="utf-8")
    env = dict(os.environ)
    for name in (
        "BEETS_PATH_DEFAULT",
        "albumartist",
        "album",
        "year",
        "track",
        "title",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(_COMPOSE),
            "config",
            "--format",
            "json",
        ],
        cwd=_COMPOSE.parent,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    rendered = json.loads(result.stdout)
    value = rendered["services"]["qobuz-librarian"]["environment"][
        "BEETS_PATH_DEFAULT"
    ]
    assert value.replace("$$", "$") == (
        "$albumartist/$album ($year)/$track - $title"
    )
    assert "variable is not set" not in result.stderr


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
    _run_entrypoint_head(tmp_path, {"CONFIG_DIR": str(cfg)})

    out = (cfg / "streamrip" / "config.toml").read_text()
    assert "downloads_enabled = false" in out and "\ndownloads_enabled = true" not in out
    assert "add_singles_to_folder = true" in out and "add_singles_to_folder = false" not in out
    assert "download_booklets = false" in out and "download_booklets = true" not in out
    # Unrelated keys are left alone.
    assert "failed_downloads_enabled = true" in out


def test_entrypoint_repairs_duplicate_enforced_keys_from_an_older_volume(tmp_path):
    cfg = _make_config(
        tmp_path,
        "[qobuz]\n"
        "download_booklets = true\n"
        "download_booklets = false\n"
        "[database]\n"
        "downloads_enabled = true\n"
        "downloads_enabled = false\n"
        "[filepaths]\n"
        "add_singles_to_folder = false\n"
        "add_singles_to_folder = true\n"
        "[misc]\n"
        "check_for_updates = true\n"
        "check_for_updates = false\n",
    )

    _run_entrypoint_head(tmp_path, {"CONFIG_DIR": str(cfg)})

    path = cfg / "streamrip" / "config.toml"
    parsed = tomllib.load(path.open("rb"))
    assert parsed["qobuz"]["download_booklets"] is False
    assert parsed["database"]["downloads_enabled"] is False
    assert parsed["filepaths"]["add_singles_to_folder"] is True
    assert parsed["misc"]["check_for_updates"] is False


def test_entrypoint_defaults_to_nonroot_user(tmp_path):
    # With no PUID/PGID the app must still drop to 1000:1000, not run as root.
    cfg = _make_config(tmp_path, "[database]\n")
    r = _run_entrypoint_head(tmp_path, {"CONFIG_DIR": str(cfg)}, capture=True)
    assert r.returncode == 0
    assert "Running as 1000:1000" in r.stdout


def test_cli_privilege_drop_canonicalises_ids_and_rejects_mixed_root(
        monkeypatch):
    from qobuz_librarian import cli

    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_in_container", lambda: True)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gosu")
    monkeypatch.setattr(os, "execvp", lambda program, argv: calls.append(
        (program, argv)))

    monkeypatch.setenv("PUID", "00")
    monkeypatch.setenv("PGID", "00")
    cli._maybe_drop_privileges()
    assert calls == []

    monkeypatch.setenv("PUID", "1000")
    monkeypatch.setenv("PGID", "00")
    with pytest.raises(SystemExit) as exc:
        cli._maybe_drop_privileges()
    assert exc.value.code == 64
    assert calls == []

    monkeypatch.setenv("PUID", "001000")
    monkeypatch.setenv("PGID", "001001")
    cli._maybe_drop_privileges()
    assert calls[0][1][1] == "1000:1001"
