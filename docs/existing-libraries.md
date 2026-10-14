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

The album folder name is flexible: `Album`, `Album (2017)`, `Album [2017]`, and `2017 - Album` all work. The year is optional. Matching locates artist and album folders, then checks track tags. A file with the same recording ID (ISRC) as a Qobuz track is that track, whatever mix or remaster label either side carries. Without a recording ID, mix and remaster labels in tags and folder names distinguish editions, while deluxe, expanded and other packaging labels do not. Per-disc subdirs (`CD1/`, `CD2/`) are recursed into; hidden directories and the staging dir are skipped. Flat (`/music/<track>.flac`) and extra-nested (`/music/<Genre>/<Artist>/...`) layouts are not detected, so point `QL_MUSIC_DIR` at the folder that contains the artist folders.

A remaster or new mix released under the same recording IDs counts as the copy you already have.

## Migrating into the layout

If your library is not already organised as artist folders with album folders inside them, the **Library migration** tool can build the expected structure. It is a one-time local-library preparation step.

It places each file by its tags (album artist, album, title, track, disc). For files whose tags are not enough to place them, an optional AcoustID fingerprint pass identifies them by sound (slower, needs network, off by default; no API key required).

Fingerprinting reads local FLAC, M4A, MP3, AAC, Ogg, Opus and WAV audio. Source installs need FFmpeg with Chromaprint support, ffprobe, and `pyacoustid` in the app's Python environment; Docker includes these.

Library migration copies by default, so the source library stays where it is. Optional move mode relocates the originals after preview and removes source folders that become empty. Cover art and sidecars next to the audio (images, `.cue`, `.log`, `.lrc`, `.pdf`, `.nfo`, playlists) are copied in both modes. Subfolders such as `Scans/` and any other files are not carried, so after a move the summary lists, folder by folder, what is still only in the source. The tool previews the plan first: how many files it can place, how many it could not place or would collide with an existing file, and how much space the destination needs (in the terminal, `--verbose` lists the files it could not place). Nothing is copied or moved until you confirm. Files it cannot confidently place are left alone and listed.

An approved migration writes two CSVs at the destination: a `migration-manifest-*.csv` recording the full plan (including everything left behind and why) and a `migration-results-*.csv` recording what the run copied, moved, skipped, or failed on. Each file gets a unique timestamped name, so repeated runs never overwrite an earlier record; the approved plan and the finished run both report the exact filename they wrote. A `--dry-run` prints the preview without writing either CSV or copying or moving music. Preview briefly creates and removes files to check filename handling, so the destination filesystem must be writable.

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

- **Web:** open **Library migration** from **Settings**, optionally tick *Fingerprint unclear files*, click **Preview migration**, review the per-artist list, then **Copy N selected** (reads **Move N selected** in move mode).
- **CLI:** `docker compose run --rm qobuz-librarian cli --migrate`. Add `--acoustid` for fingerprinting, `--in-place` to move instead of copy, or `--dry-run` to preview. Source and destination come from the env vars above or `--migrate-src` / `--migrate-dest`.

After migration finishes, point `QL_MUSIC_DIR` at the new destination and run a Library scan.

Review AcoustID matches before using the migrated library; fingerprint matches are probabilistic. A compilation with no signal at all (no compilation flag, no "Various Artists" album artist, no disc numbers) cannot be recognised as one, so each track lands under its own track artist. In the tags-only pass, the year comes from tags, so a file tagged without one, or with a placeholder year such as 1900, lands in `Artist/Album/` rather than `Artist/Album (Year)/`. In copy mode, spot-check the destination before switching `QL_MUSIC_DIR` to it. In move mode, review the preview carefully before confirming.

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

It shows the matching MusicBrainz releases one album at a time, for you to accept, skip, or replace. Lookups use beets' built-in AcoustID key; you only need your own user API key (from <https://acoustid.org/api-key>, added as `acoustid: {apikey: "KEY"}`) to submit fingerprints with `beet submit`.

Run manual beets fingerprinting only on files you trust. It uses beets' own media readers, without Library migration's audio-only restrictions.

Stop Qobuz Librarian completely before running this or any other manual `beet` command. Waiting for downloads to finish is not enough: manual commands do not participate in the app's database coordination and must not run alongside it. To also re-folder the files into the layout, use [Library migration](#migrating-into-the-layout) instead.

## The first scan on a big library

A library-wide scan fetches artist matches, catalogue pages and owned-album tracklists from Qobuz, with caches reducing repeat requests. It scans a few artists at once (`ARTIST_SCAN_WORKERS`, default 4). Qobuz rate limits are handled by automatic retry and back-off. It is scan-then-review, not a daemon. After the first scan, use the Library refresh for music added outside the app. Missing Albums lists the releases Qobuz files as each artist's albums. The review's **Live**, **Compilations** and **EPs & singles** switches add those lists without a new scan, and an album you ticked stays listed whatever its switch. The CLI's `--include-live`, `--include-comps` and `--include-singles` add them for one run (each defaults to its switch). An owned album whose only missing tracks are other versions of songs in its folder (a remix, a live take, a demo, an edit, an instrumental, a mono, stereo or single version) counts as complete; Gap Fill lists it only with the **Bonus versions** switch on, and the CLI's `--include-bonus` does the same for one run. A track counts as such a version when its title, with its version labels and Qobuz's version field set aside, matches a song in the folder and it carries a version label that song lacks. A remaster or a dated mix such as "2017 Mix" of the same song is not such a version. Reading an artist's lists takes at least four more Qobuz requests, cached for `ARTIST_CATALOG_CACHE_TTL` until the artist has a new release. An album or compilation credited first to another artist counts as a compilation, and a release shorter than `MISSING_ALBUMS_MIN_TRACKS` tracks (default 4) as an EP or single. When Qobuz does not answer for an artist, that artist's missing albums pass the older length and compilation filters instead, whatever the switches say, and the next Library refresh asks again.

Review choices are remembered, so a large library can be handled over several sessions. Library dismissals hide missing-album suggestions, Upgrade dismissals hide skipped upgrades, and Downsample remembers albums you keep hi-res. Restore them from each page's **Dismissed** view (Downsample calls it **Kept hi-res**). Saved choices are per album, so a new release by an already-reviewed artist still surfaces. Explicit single-artist CLI scans do not use the hidden list.

## After the first scan

One full scan is the setup step, not a routine. The app tracks everything it downloads on its own, so you normally never scan again. If music lands in the folders from outside the app, the small refresh icon in the Library header runs a quick pass (unchanged artist folders are skipped) and folds anything new into the review you already have open; your ticks stay put. "Force full rescan", for when you've reorganised things by hand, lives in Settings.
