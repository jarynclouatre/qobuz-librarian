# Existing libraries

[← README](../README.md)

The layout the scanner expects, and how to bring an existing collection in.

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

The album folder name is flexible: `Album`, `Album (2017)`, `Album [2017]`, `2017 - Album`, `Album - 2017` and `Artist - Album (2017)` all work, and a format tag at the end such as `[FLAC]`, `[WEB FLAC]` or `[24-96]` is ignored. The year is optional.

A file with the same recording ID (ISRC) as a Qobuz track is that track, whatever mix or remaster label either side carries. Without a recording ID, mix and remaster labels in tags and folder names distinguish editions, while deluxe, expanded and other packaging labels do not.

Per-disc subdirs (`CD1/`, `CD2/`) are recursed into; hidden directories, the staging dir, and the recycle bins and system folders NAS boxes and Windows keep are skipped, as is any folder or file whose name is not valid UTF-8, which the scan names. Those system folders are:

`#recycle`, `@Recycle`, `#snapshot`, `@Recently-Snapshot`, `@eaDir`, `$RECYCLE.BIN`, `System Volume Information`, `lost+found`

Flat (`/music/<track>.flac`) and extra-nested (`/music/<Genre>/<Artist>/...`) layouts are not detected, so point `QL_MUSIC_DIR` at the folder that contains the artist folders.

## Migrating into the layout

If your library is not already organised as artist folders with album folders inside them, the **Library migration** tool can build the expected structure.

It places each file by its tags (album artist, album, title, track, disc). For files whose tags are not enough to place them, an optional AcoustID fingerprint pass identifies them by sound (slower, needs network, off by default; no API key required).

Fingerprinting reads local FLAC, M4A, MP3, AAC, Ogg, Opus and WAV audio. Source installs need FFmpeg with Chromaprint support, ffprobe, and `pyacoustid` in the app's Python environment; Docker includes these.

Library migration copies by default. Optional move mode relocates the originals after preview and removes source folders that become empty. If a track's artwork or sidecar cannot be copied or verified at the destination, the track stays at the source for retry. Cover art and sidecars next to the audio, or in an album folder above its `CD1/` and `CD2/` folders (images, `.cue`, `.log`, `.lrc`, `.pdf`, `.nfo`, playlists), are copied in both modes. When one folder holds several albums, each album gets only the files named after one of its tracks or after the album. Subfolders such as `Scans/` and any other files are not carried, so after a move the summary lists, folder by folder, what is still in the source, and the results CSV names each file or folder.

Hidden files and folders such as a desktop trash, `.DS_Store` or macOS `._` files, and the NAS and Windows folders the scanner skips, are not read.

The tool previews the plan first: how many files it can place, how many it could not place or would collide with an existing file, and how much space the destination needs (in the terminal, `--verbose` lists the files it could not place). Files it cannot confidently place, or cannot read, are left alone and listed, as is a second copy of an album found in another folder.

An approved migration writes two CSVs at the destination: a `migration-manifest-*.csv` recording the full plan (including everything left behind and why) and a `migration-results-*.csv` recording what the run copied, moved, skipped, or failed on. Each file gets a unique timestamped name. A `--dry-run` prints the preview without writing either CSV or copying or moving music.

Preview briefly creates and removes files to check filename handling, so the destination filesystem must be writable.

Migrate also needs a destination disk that can rename a file without replacing one already there, records when folders were created, and keeps the source files' times and permissions exactly. Local Linux disks can; NFS and rclone mounts can't, and on SMB/CIFS shares and exFAT or FAT disks it depends on the share and on where the source is. Where the destination falls short, Migrate names the folder and stops before it scans the source.

Mount the source library and destination folder into the container, then set `MIGRATE_SRC` and `MIGRATE_DEST` to those container paths. Neither path may pass through a symbolic link; use the real folder. Keep the source mount read-only if you only plan to copy; move mode needs the source to be writable.

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
- **CLI:** switch to terminal mode from **Settings → Mode** ([the run lock](cli.md#the-run-lock)), then `docker compose run --rm qobuz-librarian cli --migrate`. Add `--acoustid` for fingerprinting, `--in-place` to move instead of copy, or `--dry-run` to preview. Source and destination come from the env vars above or `--migrate-src` / `--migrate-dest`.

After migration finishes, point `QL_MUSIC_DIR` at the new destination and run a Library scan.

Review AcoustID matches before using the migrated library; fingerprint matches are probabilistic. A compilation with no signal at all (no compilation flag, no "Various Artists" album artist) cannot be recognised as one, so each track lands under its own track artist. In the tags-only pass, the folder year is the one most of an album's tracks carry, so an album mostly tagged without one, or with a placeholder year such as 1900, lands in `Artist/Album/` rather than `Artist/Album (Year)/`.

## Bringing an existing beets database

The app creates `/config/beets/musiclibrary.db` the first time an import uses beets, not when the container starts. To use yours, stop the container and copy your DB and config into the `qobuz-librarian-config` volume (the DB must end up named `musiclibrary.db`):

```bash
docker run --rm -v qobuz-librarian-config:/dest -v /your/beets/dir:/src alpine \
  sh -c 'mkdir -p /dest/beets && cp /src/config.yaml /dest/beets/ && \
         cp /src/library.db /dest/beets/musiclibrary.db'
```

Replace `library.db` with your filename (check the `library:` path in your `config.yaml` if unsure), or bind-mount a host directory at `/config/beets` instead. The container will not overwrite either file on start. If renaming the DB to `musiclibrary.db` is not convenient, point `BEETS_DB_PATH` at your file instead (e.g. `BEETS_DB_PATH=/config/beets/library.db`) and the app reads it from there.

Keep the database on a local filesystem: the default Compose config volume or a local bind mount, not NFS or SMB/CIFS. See [Configuration](configuration.md#beets--streamrip-config) for details.

## Optional beets fingerprinting

Qobuz downloads arrive fully tagged, so import leaves the autotagger off. If you are bringing in older untagged files, beets' `chroma` plugin can identify them by audio fingerprint (AcoustID) and tag them in place (`fpcalc` ships in the image). A ready-made config keeps that optional pass separate from your normal beets settings:

```bash
docker compose run --rm qobuz-librarian \
  beet -c /app/docker/beets-chroma.yaml import /music/<your-untagged-folder>
```

It shows the matching MusicBrainz releases one album at a time, for you to accept, skip, or replace. Lookups use beets' built-in AcoustID key; you only need your own user API key (from <https://acoustid.org/api-key>, added as `acoustid: {apikey: "KEY"}`) to submit fingerprints with `beet submit`.

Run manual beets fingerprinting only on files you trust. It uses beets' own media readers, without Library migration's audio-only restrictions.

Stop Qobuz Librarian completely before running this or any other manual `beet` command. Waiting for downloads to finish is not enough: manual commands do not participate in the app's database coordination and must not run alongside it. To also re-folder the files into the layout, use [Library migration](#migrating-into-the-layout) instead.

## How editions are matched

Library scans group editions of the same album. Search marks an edition **In library** when your files hold its recordings: the same recording ID (ISRC), or for files without one, the same title and mix or remaster label; deluxe and other packaging labels do not separate editions. Gap Fill fills a folder from the edition it holds, matched the same way, and leaves out a song the folder already has in another version.

It offers nothing for a folder when Qobuz lists only another master or mix of it, or when more than one in ten of the songs the folder has from that edition are another version. An edition named for a version, such as Remixes, Acoustic or Deluxe Edition, is kept apart from a folder whose name lacks that version and that holds none of its remixes, demos or other versioned tracks.

An album whose only missing tracks are other versions of songs you have, such as remixes, live takes or demos, counts as complete: Search marks it **In library** with a note of the bonus versions, and Gap Fill lists it only with the review's **Bonus versions** switch on.

Other pressings fold under **N other versions**. **Download** adds an edition's missing tracks to the folder that holds its other tracks; when no folder holds that edition, it downloads as a separate copy beside the version you have.

Upgrade also offers an album you have as MP3, AAC, Ogg Vorbis or Opus when Qobuz has the same edition in FLAC, and replaces those files with FLACs.

Upgrade replaces files only once each one is matched to its new copy, so it does not offer a folder where a file has no ISRC while Qobuz's tracks have one, or the other way round.

## The first scan on a big library

A library-wide scan fetches artist matches, catalogue pages and owned-album tracklists from Qobuz, with caches reducing repeat requests. It scans a few artists at once (`ARTIST_SCAN_WORKERS`, default 4). Qobuz rate limits are handled by automatic retry and back-off. After the first scan, use the Library refresh for music added outside the app.

Missing Albums lists the releases Qobuz files as each artist's albums, plus releases of `MISSING_ALBUMS_MIN_TRACKS` or more tracks (default 4) credited to the artist that Qobuz files as download-only. An album or compilation credited first to another artist counts as a compilation, and a release shorter than `MISSING_ALBUMS_MIN_TRACKS` tracks as an EP or single.

The review's switches add the other lists without a new scan. The CLI flags add them for one run (each defaults to its switch).

- **Live**: `--include-live`
- **Compilations**: `--include-comps`
- **EPs & singles**: `--include-singles`
- **Other releases**: `--include-other`. The artist's releases of `MISSING_ALBUMS_MIN_TRACKS` or more tracks that Qobuz files under other: mostly label reissues and collections, sometimes an album of its own.

An owned album whose only missing tracks are other versions of songs in its folder (a remix, a live take, a demo, an edit, an instrumental, a mono, stereo or single version) counts as complete; Gap Fill lists it only with the **Bonus versions** switch on, and the CLI's `--include-bonus` does the same for one run. A remaster or a dated mix of the same song, such as "2009 Remaster", "2017 Mix" or "Stereo Mix 2015", is not such a version; a remastered mono or stereo version, such as "Mono Remastered", is.

Review choices are remembered, so a large library can be handled over several sessions. Library dismissals hide missing-album suggestions, Upgrade dismissals hide skipped upgrades, and Downsample remembers albums you keep hi-res. Bring them back from each page's **Dismissed** view (Downsample calls it **Kept hi-res**). Saved choices are per album, so a new release by an already-reviewed artist still surfaces. Explicit single-artist CLI scans do not use the hidden list.

## After the first scan

One full scan is enough: the app tracks everything it downloads. For music added outside the app, the refresh icon in the Library header runs a quick pass (unchanged artist folders are skipped) and adds anything new to the open review. **Force full rescan** is in Settings.
