# Existing libraries

[← README](../README.md)

The defaults assume a fresh library. This covers the layout the scanner expects, and how to bring an existing collection in.

## Folder layout

The scanner expects a two-level tree under your music library. In Docker, this is the folder set by `QL_MUSIC_DIR`; inside the container it is mounted at `/music`. The folder should contain artist folders, with album folders inside them:

```
/music/
├── Artist Name/
│   ├── Album Name/
│   │   └── 01 - Track.flac
│   └── Album (2017)/
│       ├── CD1/
│       └── CD2/
└── Other Artist/
    └── 2017 - Album/
        └── 01 - Track.flac
```

The album folder name is flexible: `Album`, `Album (2017)`, `Album [2017]`, and `2017 - Album` all work. The year is optional; matching uses track tags, not folder names. Per-disc subdirs (`CD1/`, `CD2/`) are recursed into; hidden directories and the staging dir are skipped. Flat (`/music/<track>.flac`) and extra-nested (`/music/<Genre>/<Artist>/...`) layouts are not detected, so point `QL_MUSIC_DIR` at the folder that contains the artist folders.

## Migrating into the layout

If your library is not already organised as artist folders with album folders inside them, the **Migrate** tool can build the expected structure. It is a one-time local-library preparation step.

It places each file by its tags (album artist, album, title, track, disc). For files whose tags are not enough to place them, an optional AcoustID fingerprint pass identifies them by sound (slower, needs network, off by default; no API key required).

Migrate copies by default, so the source library stays where it is. Optional move mode relocates the originals after preview and removes source folders that become empty. The tool previews the full plan first: where each file goes, what it could not place, and how much space is needed at the destination. Nothing is copied or moved until you confirm. Files it cannot confidently place are left alone and listed.

Two CSVs land at the destination: a `migration-manifest-*.csv` recording the full plan (including everything left behind and why) and a `migration-results-*.csv` recording what the run copied, moved, skipped, or failed on. Each file gets a unique timestamped name, so repeated runs never overwrite an earlier record; the preview and the finished run both report the exact filename they wrote.

Mount the source library and destination folder into the container, then set `MIGRATE_SRC` and `MIGRATE_DEST` to those container paths. Keep the source mount read-only if you only plan to copy; move mode needs the source to be writable.

```yaml
services:
  qobuz-librarian:
    environment:
      MIGRATE_SRC: /migrate-source
      MIGRATE_DEST: /migrate-dest
    volumes:
      - /path/to/your/current/library:/migrate-source:ro # remove :ro for move mode
      - /path/to/new/empty/folder:/migrate-dest
```

Then either:

- **Web:** open **Migrate**, optionally tick *Fingerprint unclear files*, click **Preview migration**, review the per-artist list, then **Copy N selected** (reads **Move N selected** in move mode).
- **CLI:** `docker compose run --rm qobuz-librarian cli --migrate`. Add `--acoustid` for fingerprinting, `--in-place` to move instead of copy, or `--dry-run` to preview. Source and destination come from the env vars above or `--migrate-src` / `--migrate-dest`.

After migration finishes, point `QL_MUSIC_DIR` at the new destination and run a Library scan.

Review AcoustID matches before using the migrated library; fingerprint matches are probabilistic. A compilation with no signal at all (no compilation flag, no "Various Artists" album artist, no disc numbers) cannot be recognised as one, so each track lands under its own track artist. The year comes from tags only, so a file tagged without one lands in `Artist/Album/` rather than `Artist/Album (Year)/`. In copy mode, spot-check the destination before switching `QL_MUSIC_DIR` to it. In move mode, review the preview carefully before confirming.

## Bringing an existing beets database

The app creates `/config/beets/musiclibrary.db` the first time an import uses beets, not when the container starts. To use yours, stop the container and copy your DB and config into the `qobuz-librarian-config` volume (the DB must end up named `musiclibrary.db`):

```bash
docker run --rm -v qobuz-librarian-config:/dest -v /your/beets/dir:/src alpine \
  sh -c 'mkdir -p /dest/beets && cp /src/config.yaml /dest/beets/ && \
         cp /src/library.db /dest/beets/musiclibrary.db'
```

Replace `library.db` with your filename (check the `library:` path in your `config.yaml` if unsure), or bind-mount a host directory at `/config/beets` instead. The container will not overwrite either file on start. If renaming the DB to `musiclibrary.db` is not convenient, point `BEETS_DB_PATH` at your file instead (e.g. `BEETS_DB_PATH=/config/beets/library.db`) and the app reads it from there.

The default Compose config volume provides the local Linux filesystem guarantees the database needs. If you replace it, run the app on Linux with `/proc/self/fd` and use a local mount that provides hard links, xattrs, advisory `flock`, file leases, atomic `renameat2` exchange, and file and directory `fsync`; do not place the database on NFS or SMB/CIFS. See [Configuration](configuration.md#beets--streamrip-config) for details.

## Optional beets fingerprinting

Qobuz downloads arrive fully tagged, so import leaves the autotagger off. If you are bringing in older untagged files, beets' `chroma` plugin can identify them by audio fingerprint (AcoustID) and tag them in place (`fpcalc` ships in the image). A ready-made config keeps that optional pass separate from your normal beets settings:

```bash
docker compose run --rm qobuz-librarian \
  beet -c /app/docker/beets-chroma.yaml import /music/<your-untagged-folder>
```

It shows the matching MusicBrainz releases one album at a time, for you to accept, skip, or replace. Lookups use beets' built-in AcoustID key; you only need your own (from <https://acoustid.org/new-application>, added as `acoustid: {apikey: "KEY"}`) to submit fingerprints with `beet submit`.

Stop Qobuz Librarian completely before running this or any other manual `beet` command. Waiting for downloads to finish is not enough: manual commands do not participate in the app's database coordination and must not run alongside it. To also re-folder the files into the layout, use [Migrate](#migrating-into-the-layout) instead.

## The first scan on a big library

A library-wide scan makes roughly one Qobuz call per artist directory (cached on re-scans, so repeated scans mostly use cached data), fanned across a few artists at once (`ARTIST_SCAN_WORKERS`, default 4). There is no artificial delay between calls (`ARTIST_API_DELAY`, default 0); Qobuz's rate limit is handled by automatic retry and back-off, so raise it only if you get throttled. It is scan-then-review, not a daemon. After the baseline, use the Library refresh for music added outside the app. Singles and very short EPs are hidden from the missing-albums step by default; lower `MISSING_ALBUMS_MIN_TRACKS` (e.g. to 1) to surface them. (A single-artist run can also pass `--include-singles`.)

Review choices are remembered, so a large library can be handled over several sessions. Library dismissals hide missing-album suggestions, Upgrade dismissals hide skipped upgrades, and Downsample remembers albums you keep hi-res. Restore them from each page's **Dismissed** view (Downsample calls it **Kept hi-res**). Saved choices are per album, so a new release by an already-reviewed artist still surfaces. Explicit single-artist CLI scans do not use the hidden list.

## After the baseline

One full scan is the setup step, not a routine. The app tracks everything it downloads on its own, so you normally never scan again. If music lands in the folders from outside the app, the small refresh icon in the Library header runs a quick pass (unchanged artist folders are skipped) and folds anything new into the review you already have open; your ticks stay put. "Force full rescan", for when you've reorganised things by hand, lives in Settings.
