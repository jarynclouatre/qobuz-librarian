# Configuration

[← README](../README.md)

Host paths and the web port come from `.env`, which is gitignored. Start by copying `.env.example`.

Docker Compose reads those values on the host and maps them to the container paths used by the app. Compose only passes a variable into the container when `compose.yaml` lists it under `environment:`.

Use the **Settings** page for day-to-day behaviour:

- download quality
- lyrics and artwork
- beets folder layout
- scan cadence

These settings apply to new jobs. A field you change on the Settings page wins over `.env` for that field; fields you never change there keep following `.env`. The Qobuz token is the other way round: one set in the environment wins over one saved on Settings. Timeouts, worker counts, pacing tunables, `WEB_AUTH*`, host bind, and `LOG_LEVEL` stay in `.env` or `compose.yaml` and may need a restart.

## Host paths

| Variable | Default | Purpose |
|---|---|---|
| `QL_MUSIC_DIR` | `./music` | Music library; beets imports into this |
| `QL_STAGING_DIR` | `./staging` | Scratch space for in-progress downloads |
| `QL_UPGRADE_BACKUPS` | `./upgrade_backups` | Backups of files Upgrade, Repair and the other tools replace |
| `QL_COLLECTION_BACKUPS` | `./collection_backups` | Collection snapshots used by Backup & Restore |
| `WEB_PORT` | `8666` | Host port for the web UI |

The bundled `compose.yaml` also hands the folder values you set here to the app, so Settings and Diagnostics can name the host folder. A folder left at its default, or set some other way, shows as the path inside the container, such as `/music`.

Outside Docker the app reads its folders from the environment instead:
`MUSIC_ROOT` (default `~/Music`), `STAGING_DIR` and `UPGRADE_BACKUP_DIR`
(`.staging` and `.upgrade_backups` beside the music folder), `DATA_DIR`
(`~/.local/share/qobuz-librarian`), `COLLECTION_BACKUP_DIR`
(`collection-backups` in the data folder), `BEETS_CONFIG_DIR`
(`~/.config/beets`), `BEETS_DB_PATH` (`musiclibrary.db` in the beets folder,
or beets' own `library.db` there when only that one exists), and
`STREAMRIP_CONFIG` (`~/.config/streamrip/config.toml`). A relative folder is
taken from the directory you start the app in. If your beets library is
somewhere else, set `BEETS_DB_PATH` to it.

The music, staging, and upgrade-backup directories must be separate,
non-nested trees. The app refuses to start if any one is the same as, inside,
or an alias of another.

Give each of these folders, and the data and beets folders, by its real
path. If a path goes through a link, whether the folder itself or one above
it, the app refuses to start and names the real folder to use. If that
changes the music folder, run **Force full rescan** on Settings once so the
Library review picks up the new path.

## Behaviour toggles

| Variable | Default | Purpose |
|---|---|---|
| `STREAMRIP_QUALITY` | `4` | Download tier 2–4 (see [Download quality](#download-quality)) |
| `PREFER_HIRES` | `true` | When a release has several versions, pick the hi-res master rather than the original edition. Upgrade only offers the edition you have. |
| `LYRICS_ENABLED` | `true` | Fetch lyrics on import |
| `LYRICS_FORMAT` | `embed` | `embed` (FLAC tag), `sidecar` (.lrc), or `both` |
| `LYRICS_PROVIDERS` | unset | Ordered comma list of `Lrclib`, `NetEase`, `Megalobiz`, `Musixmatch` and `Genius`; unset tries `Lrclib` and `Musixmatch`, in that order. NetEase is asked at most 15 times a minute, so a pass over a large library takes hours with it. Other names are ignored with a warning at startup; with no known name, no lyrics are fetched |
| `LYRICS_PROVIDER_TIMEOUT` | `30` | Maximum seconds for one provider lookup before its circuit-breaker records a failure |
| `ARTWORK` | `both` | Cover art: `sidecar`, `embed`, or `both` |
| `COLLECTION_BACKUP_DIR` | `/collection_backups` in Compose; app data otherwise | Container path for collection backups from completed Library scans and terminal Library walks and Gap Fill runs |
| `LASTFM_API_KEY` | unset | Enables the Discover tab; without it the tab does not appear. Also settable on the Settings page, and `LASTFM_API_KEY_FILE` reads it from a file |
| `AUTO_LIBRARY_SCAN` | `true` | Offer the first Library scan on the Search page on first run, and auto-resume an interrupted library scan when the app is idle (`false` turns both off; the manual Resume button still works) |
| `NEW_RELEASE_CHECK_INTERVAL` | `86400` | How often (seconds) to auto-check for new releases, at least 900; daily (also on Settings) |
| `NEW_RELEASE_MAX_AGE_DAYS` | `365` | Only flag albums released within this many days as new (`0` = any catalogue newcomer counts, including older releases Qobuz recently added) |
| `ARTIST_CATALOG_CACHE_TTL` | `604800` | How long (seconds) artist album-lists stay cached; 7 days (also on Settings) |
| `REPAIR_CACHE_ENABLED` | `true` | Cache the repair scan's Qobuz ISRC lookups (files are still decode-tested fresh every scan) |
| `REPAIR_CACHE_TTL_DAYS` | `30` | How long a cached ISRC lookup is reused before re-verifying (`0` = keep until the db is deleted) |
| `UPGRADE_SCAN_ENABLED` | `true` | Show Upgrade and include the quality-upgrade pass in Library scans (`false` hides Upgrade and skips that pass) |
| `AUTO_UPGRADE_ENABLED` | `false` | Let an ordinary CLI download or gap-fill run also upgrade an album Qobuz can now serve better in the same edition, after confirmation. Ordinary web downloads keep the selected edition; use the Upgrade page to review and approve web replacements. |
| `DOWNSAMPLE_HIRES_ENABLED` | `false` | Downsample FLACs above 48 kHz as they download (see below) |
| `UPGRADE_SINGLES_ENABLED` | `false` | Let the Upgrade walk re-rip tracks you pulled as singles |
| `MIGRATE_MULTI_ARTIST` | `true` | Re-file `A, B/Album` or `B, A/Album` under `A/Album` after import, where `A` is the album's main artist on Qobuz |
| `SUPPRESS_SINGLE_TRACK_GAPS` | `false` | Web Library scans and new-release checks don't offer the rest of an album you took tracks from with a Search **Track** download, and skip an artist whose only folders came from such downloads. Folders that hold one track for any other reason are scanned as usual, and the CLI walks ignore this setting |
| `EXCLUDE_LIVE_ALBUMS` | `false` | Count a release whose title marks it live, on tour, a session or acoustic as a live release in Missing Albums even when Qobuz files it as an album or compilation, so it shows only with **Live** switched on; a studio album whose real title merely contains "Live" is never matched |
| `CONSOLIDATE` | `false` | Merge sibling/duplicate album folders (CLI-only) |

`MIGRATE_MULTI_ARTIST` affects new imports only, including a gap fill or upgrade of an album already in a combined folder; a Search **Track** download stays in its album's folder. It never replaces a name that already exists under the primary artist; an album with such a name stays in the combined folder.

`DOWNSAMPLE_HIRES_ENABLED` only touches new downloads (88.2 / 176.4 / 352.8 kHz → 44.1; 96 / 192 / 384 → 48; bit depth preserved). To downsample hi-res already in your library, use **Downsample** instead. While it is on, Upgrade compares your files with what a download keeps once downsampled, so a downsampled album is not offered its hi-res master again.

Pacing, timeout, and worker knobs are forwarded in `compose.yaml`, so setting them in `.env` takes effect (each is described in `.env.example`):

- `RIP_TIMEOUT`
- `DELAY_BETWEEN`
- `ARTIST_API_DELAY`
- `ARTIST_SCAN_WORKERS`
- `MISSING_ALBUMS_MIN_TRACKS`
- `BEETS_TIMEOUT`
- `MIN_FREE_STAGING_MB`
- `RESAMPLE_WORKERS`

Fuzzy-match cutoffs, search and catalogue limits, and the job-log and stream
internals are constants in `src/qobuz_librarian/config.py`, not settings.
`POST_JOB_HOOK` runs in a shell, so only set it to a command you trust.

## Download quality

Defaults to tier `4`, the best Qobuz serves per release (24-bit up to 192 kHz, else CD lossless). Change it on **Settings** or via `STREAMRIP_QUALITY`.

| Tier | Quality | Notes |
|---|---|---|
| `4` | 24-bit ≤192 kHz | Default; archival |
| `3` | 24-bit ≤96 kHz | Hi-res, smaller cap |
| `2` | 16-bit / 44.1 kHz | CD lossless, smallest |

When Qobuz sends a track as a broken file at tier `3` or `4` but its CD-quality file is whole, that one track comes at CD quality instead of going missing; never lossy. The log and History name it.

To keep hi-res masters smaller, pull tier `4` and either enable import-time downsampling (`DOWNSAMPLE_HIRES_ENABLED`) or run **Downsample** on demand.

## beets & streamrip config

The bundled tools' full config files live in the persistent `config` volume. They are seeded on first run; the app maintains its required streamrip options and saved Qobuz credentials:

- `…/beets/config.yaml`: tagging, paths, plugins ([beets docs](https://beets.readthedocs.io/))
- `…/streamrip/config.toml`: downloader settings ([streamrip docs](https://github.com/nathom/streamrip))

Each download runs streamrip with a private copy of its config. The copy holds the Qobuz credentials in use and sets the staging folder, `Disc N` folders, each single in its own folder, no booklets, no downloads database and no update check. Downloads never change the streamrip config itself; each container start sets the options the app needs in it, and saving credentials on **Settings** writes them to it.

Keep the beets database on a local filesystem, such as the default Compose `/config` named volume or a local bind mount. NFS, SMB/CIFS, and other network filesystems are not supported for the database; the music library itself may still live on network storage, within the limits under [Permissions](#permissions-nas-and-shared-storage).

Set folder and file naming with `BEETS_PATH_DEFAULT` and `BEETS_PATH_COMP` on the **Settings** page or in `.env`. These use beets path syntax, for example `$albumartist/$album ($year)/$track - $title`. Enter that raw template on the Settings page. In `.env`, single-quote the whole assignment so Compose does not consume the beets variables:

```dotenv
BEETS_PATH_DEFAULT='$albumartist/$album ($year)/$track - $title'
```

Set the plugins you choose with `BEETS_PLUGINS`. When set, this list replaces the plugins selected in `config.yaml` for imports run by Qobuz Librarian, and may name your own plugins in the folders `config.yaml` lists under `pluginpath`. The app then adds `inline` for its multi-disc folder field, its internal import guards, and `fetchart` unless `ARTWORK` is `embed`, which leaves it out. Plugin settings, such as the replaygain backend, go in `config.yaml`. Extra plugins may need dependencies not bundled in the image; [lastgenre](https://beets.readthedocs.io/en/stable/plugins/lastgenre.html), for example, needs `beets[lastgenre]==2.14.1` installed in the same Python environment. Docker users need a custom image for those additions.

For a `pip` or `pipx` installation, install beets 2.14.1 in the same environment. Qobuz Librarian normally finds that environment from the `beet` launcher. If the launcher is an unusual wrapper, set `BEETS_PYTHON` to the absolute path of the Python executable in that environment. Other beets versions are refused.

Treat enabled beets plugins as trusted code. A plugin must finish all database work before its `beet` command exits. Stop Qobuz Librarian completely before running a manual `beet` command.

For its own imports, the downloader pins these beets settings regardless of your config:

- `autotag: no` keeps Qobuz's tags
- `write: no` prevents beets from rewriting media tags during the move
- `move: yes` clears staging, including across filesystems
- `incremental: no` rescans on retry
- `duplicate_action: merge` keeps an import from deleting files already in your library
- `singletons: no` imports each download as one album
- `timeout: 60` waits up to a minute for another program using the beets database, such as `beet web` (half of a nonzero `BEETS_TIMEOUT` below two minutes)

It also handles these itself:

- Gap fills and upgrades use the album's existing folder while it remains in place, with files named by your path template. The added tracks take the album-level tags the album's tracks share, such as its name, album artist, date, genre and disc count, so they join its beets entry. If beets would rename that folder, as its default `replace` rules do to a trailing dot, the tracks are not imported and stay in staging recovery.
- A release tagged as a compilation or credited to Various Artists is filed with the compilation template.
- `ARTWORK` works whatever plugins you choose. With `sidecar` the downloaded tracks carry no art. Tracks already in your library keep theirs, and a Repair refill keeps any art its original track had. With `embed` or `both`, the cover the download saved is embedded in any track that has none before beets imports it. With `sidecar` or `both`, `fetchart` files that cover from the album folder; when tracks are added to an album's existing folder, the download's cover is filed only when the album has none. `embed` leaves no cover file. An upgrade or whole-album Repair of an album that has its own cover file under the same name, such as `cover.jpg`, uses that file instead of the download's.
- `filefilter` is not loaded, since it would drop tracks from a download.
- Before beets updates an older database to its current schema, one copy of it is kept beside it, named with `-before-migrations.bak` added.

Your own `beet` commands read your config unchanged. Edits apply on the next import, no restart.

## Permissions (NAS and shared storage)

The container runs as `PUID:PGID` (default `1000:1000`). If your host or media share owner is not `1000`, set them in `.env`:

```bash
PUID=1000   # id -u
PGID=1000   # id -g
```

On boot, the container chowns its managed config folders and the `data` and `staging` volumes to that user, and the `upgrade_backups` and `collection_backups` folders themselves but not the music files and collection snapshots kept inside them, and warns if a mounted path is not writable. `/music` is left alone; the run user must be able to write to it.

Operations that replace or remove an existing library file also require the share to flush file and directory changes reliably. If the mount cannot do that, Qobuz Librarian stops the destructive step and keeps the original.

The app tries the disk under the music, staging and upgrade backup folders once, while it holds the library, and **Settings → Diagnostics** says what each one can't do. A feature whose folder can't do what it needs stops before it starts and names the folder:

- Downloads need a staging folder that can rename a file without replacing one already there and swap a file for its new copy in one step.
- A new or whole lossless album downloaded while import-time Downsample is off also needs a music folder that can rename without replacing. Elsewhere that album is left out before it downloads; single tracks, partial albums and albums downloaded with Downsample on still go ahead.
- Upgrade, Gap Fill, Repair, Re-download and Consolidation move the tracks they replace to a backup and put them back if the step fails, which needs music and upgrade backup folders that can rename without replacing. Upgrade also needs a music folder that can swap files, and Upgrade and Repair one whose disk records when folders were created.
- Lyrics needs a music folder that can rename without replacing, and with `LYRICS_FORMAT` set to `embed` or `both` one that can swap files too.
- Downsample needs a music folder that can swap files.
- Albums picked from a Library, Upgrade, Downsample or Repair scan, and album downloads that fill gaps in an album already in the library, need a music folder whose disk records when folders were created.

Local Linux disks such as ext4, Btrfs and XFS can do all of this. NFS and rclone mounts can't rename safely, and rclone and many other FUSE mounts don't record when folders were created. SMB/CIFS shares and exFAT disks can rename without replacing but can't swap files. Keep staging on a local disk. A music folder on a share still works for scans, and for what the list above allows.

For a read-only music share, append `:ro` to the `/music` bind: scans run, downloads are off, and the tools skip files they can't change. A music or staging folder the container user can't write to pauses downloads and scans until it is fixed. Fixing the mount or its ownership takes effect without a restart.

If the music folder was auto-created as root on a first `up`, chown it; the container fixes its own volumes on the next start:

```bash
sudo chown -R 1000:1000 ./music
```

To run as root, set `PUID=0` and `PGID=0` explicitly. A non-numeric value makes the container refuse to start.

Migration keeps each file's permissions and modification time. Its copies take the app's ownership and do not carry extended attributes or Linux ACLs. Unless the app runs as root, backup copies, originals put back from them, and files rewritten by Downsample or a lyrics write belong to the app's user; they keep the file's group when that user is a member of it.

## Timezone

Set `TZ` in `.env` (an IANA name like `America/Edmonton`) so exact timestamps in History and on job pages show your local time instead of UTC. Relative labels like "2 hr ago" are correct either way.

## Deployment

**Login.** The web UI requires sign-in out of the box. The password is stored as a salted PBKDF2 hash (`0600`, never plaintext) and the session is an `HttpOnly` cookie backed by a persisted token digest. Its name, and the form-security cookie's, carry the port in the address (`ql_session_8666` for `http://nas:8666`), so two instances reached by one host name keep separate sign-ins. New and reset passwords need at least 15 characters, and common or product-name passwords are refused. Set the credentials on the first-visit screen, or seed `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` in `.env`. Those two double as a password reset: change them and restart. The same policy applies when the password comes from `WEB_AUTH_PASSWORD_FILE`. To reset without env vars, stop the container, delete `.qobuz_web_auth.json` from the data volume, and set new credentials on the next visit.

`WEB_AUTH=none` disables login. Use it only on a trusted LAN or behind your own authenticating proxy. The container logs a warning every boot while login is off.

**Host names.** With `WEB_AUTH=none`, and on the first-visit setup screen, the web UI answers only to an IP address, `localhost`, a single-label name such as `nas`, a name ending in `.local`, `.lan`, `.home`, `.home.arpa`, `.internal`, `.localdomain` or `.fritz.box`, or a name listed in `WEB_ALLOWED_HOSTS` (comma-separated, without scheme or port). Any other name gets a plain 400 page naming that setting. Requests from a proxy named in `FORWARDED_ALLOW_IPS` are not checked. State-changing requests the browser reports as coming from another site are refused with a 403, CSRF token or not.

**Container probes.** `/healthz` is a process-liveness check. Docker uses `/readyz`, which returns 503 when an existing login cannot be read, the data directory or Queue/History database is unavailable, saved jobs could not be restored, the run lock is unsafe or lost, or shutdown has started. Deliberate write pauses such as terminal mode, another active run, recovery, or read-only music and staging volumes return 200 with `status: degraded`. Both routes are available without signing in and return only category names, never paths or credentials.

**Behind a reverse proxy.** Set `FORWARDED_ALLOW_IPS` to the address the app sees the proxy connect from: in Docker, the network's gateway (often `172.18.0.1`) for a proxy on the host, or the proxy container's address on a shared network; with host networking or outside Docker, `127.0.0.1` for a proxy on the same machine (add `::1` if `WEB_HOST` is `::`). Ranges such as `172.18.0.0/16` work for a proxy container whose address changes. Trusting the gateway also trusts anything that reaches the published port through the host, so pair it with `WEB_BIND=127.0.0.1`. It decides three things: the failed-login throttle counts attempts per real client rather than per the shared proxy IP, a request the proxy received over HTTPS is recognised as such, which is what marks the session cookie `Secure`, and the proxy's requests skip the host-name check above. Point it at your proxy, not `*`. By default no proxy is trusted, so leaving it unset behind a proxy throttles every visitor as one.

The web UI needs its own host name or port; it can't be served under a path such as `example.com/qobuz`. In Nginx Proxy Manager, leave **Block Common Exploits** off for it: that option refuses searches for some words, such as "cocaine".

**Keeping the token out of the environment.** `docker inspect` exposes environment variables, so to keep the Qobuz token out of them, point `QOBUZ_USER_AUTH_TOKEN_FILE` at a file containing only the token (a [Docker secret](https://docs.docker.com/engine/swarm/secrets/) or read-only bind mount) instead of setting `QOBUZ_USER_AUTH_TOKEN`. The web-login password supports the same pattern with `WEB_AUTH_PASSWORD_FILE`.

**Hardening.** The bundled `compose.yaml` ships with `mem_limit: 1g`, `pids_limit: 256`, `no-new-privileges`, `cap_drop: [ALL]`, and `0600` token files. It adds back only the capabilities needed for the PUID/PGID handover. The built-in login is a single shared credential with brute-force limiting (after 5 failures an hour from one address, its sign-ins get a 429 with the login page until 15 minutes after the last failure; each failed sign-in and each lockout is logged as a warning with the client address), which is appropriate for a trusted network; use a proxy, VPN, or Tailscale for internet exposure. The image is multi-arch (`linux/amd64`, `linux/arm64`), so arm64 NAS boxes run natively, and a `--read-only` rootfs works with `--tmpfs /tmp`. For a `/var/tmp` mount instead, set `APP_HOME=/var/tmp` in `.env` and use `--tmpfs /var/tmp`. See [SECURITY.md](../SECURITY.md).

## Notifications

`POST_JOB_HOOK` runs a command of your choice every time a background job finishes and when a scan parks results for you to review. The command runs inside the container, which bundles `curl` and Python. Its environment is the app's without `WEB_AUTH_PASSWORD`, `QOBUZ_USER_AUTH_TOKEN`, `LASTFM_API_KEY` or their `_FILE` forms. `POST_JOB_HOOK_TIMEOUT` (default 10s) caps a slow endpoint, and a command that cannot start, times out, or exits nonzero is recorded in the application log.

The job's state arrives as JSON on stdin (`id`, `status`, `title`, `edition`, `display_title`, `artist`, `error`, `tracks_missing`, `finished_at`, `kind`, `summary`, `review_items`). `status` is `done`, `failed` or `canceled` for a finished job; `awaiting_review` when a Library scan, new-release check, or Upgrade, Downsample or Repair scan has results waiting, with `review_items` saying how many; and `interrupted` for a job a restart cut short. `kind` names the work (`download`, `library`, `new_releases`, `upgrade`, `downsample`, `repair`, `lyrics`, `migration`, `collection_snapshot`, `collection_restore`), and `summary` is the one-line result shown in History. An album that imported with tracks still missing finishes as `done` with `tracks_missing` above 0. Terminal runs report their album downloads and their Upgrade, Repair and Downsample runs; terminal Lyrics and Library migration runs are not reported.

The hook must not run `beet` or otherwise edit its database while Qobuz Librarian is running.

The simplest form pushes the raw JSON to an [ntfy](https://ntfy.sh) topic, straight from `.env`:

```bash
POST_JOB_HOOK=curl -s -o /dev/null -H "Title: Qobuz Librarian" -d @- https://ntfy.sh/your-topic
```

For a readable message, point the hook at a small script in the config volume instead (`POST_JOB_HOOK=/config/notify.sh`, made executable with `chmod +x`):

```sh
#!/bin/sh
# ntfy, one line per job: "done: Bonobo - Black Sands (14 tracks downloaded.)"
msg=$(python3 -c 'import json,sys
j = json.load(sys.stdin)
line = j["status"] + ": " + " - ".join(x for x in (j.get("artist"), j.get("title")) if x)
detail = j.get("error") or j.get("summary")
print(line + (" (" + detail + ")" if detail else ""))')
curl -s -o /dev/null -H "Title: Qobuz Librarian" -d "$msg" https://ntfy.sh/your-topic
```

The same shape works for a Discord webhook; build the JSON body and post it:

```sh
#!/bin/sh
python3 -c 'import json,sys
j = json.load(sys.stdin)
line = j["status"] + ": " + " - ".join(x for x in (j.get("artist"), j.get("title")) if x)
print(json.dumps({"content": "Qobuz Librarian: " + line}))' \
  | curl -s -o /dev/null -H "Content-Type: application/json" -d @- "https://discord.com/api/webhooks/YOUR/WEBHOOK"
```

Anything that accepts an HTTP request works the same way: Gotify, Slack, Home Assistant webhooks, or a plain script that writes to a file.

The hook also fires once if the saved Qobuz token stops being accepted (`status` is `auth_lost`).

## What the app does on its own

On first run the Search page offers a first Library scan (`AUTO_LIBRARY_SCAN`). Once a full scan has finished, new-release checks (`NEW_RELEASE_CHECK_INTERVAL`) run on a background timer while nothing else is running, the first one an interval after that scan. A pre-order is listed once its release date has passed, and an artist with more than 1,000 albums on Qobuz is checked from their newest releases alone. Both park a review list.

- **Library gap-fill** adds missing albums or missing tracks after review.
- **After a download**, it re-checks the new album's track lengths against Qobuz, and the download's log says to run **Repair** if one is short.
- **Upgrade** and **Downsample** change files only when you start them. Upgrade backs up the original album and removes that backup only after replacement and catalogue checks pass. If verification fails, it restores the original where safe; otherwise the backup remains for review. A backup the upgrade keeps is listed under Settings → Diagnostics and stays until you restore or remove it. An album still in its backup, or part way into it, because the app or a terminal run was stopped before the replacement was imported is put back the next time either one starts, provided its folder is still missing and the folders above it are the same ones. Downsample rewrites in place after verifying each file decodes, or, with *Keep originals when downsampling* set to keep (`DOWNSAMPLE_KEEP_ORIGINALS`), parks the hi-res copies in the backup area first so the rewrite can be undone from Settings → Diagnostics until the retention window (`UPGRADE_BACKUP_RETENTION_DAYS`) ends, as long as the album still holds the files Downsample wrote.
- **Lyrics** writes tags or `.lrc` sidecars, not the audio. A track without lyrics gets synced ones, or plain ones when no synced lyrics are found and *Synced lyrics only* is off. Plain lyrics are replaced when synced ones are found. Synced lyrics are never replaced, and no lyrics are removed, though in `sidecar` format the tag lyrics of newly imported tracks move into `.lrc` files. A lookup that finds nothing new is repeated after 30 days, or sooner with *Re-check everything* (`--lyrics-rescan`).
- **Consolidation** (`CONSOLIDATE`, off) merges duplicate folders, CLI-only (it needs per-folder confirmation). It offers only a folder whose tracks are all in the album just downloaded. The duplicate tracks it takes out wait in the backup area and are cleared after `UPGRADE_BACKUP_RETENTION_DAYS`, as long as the album kept in their place still holds a playable track at least as long for each.
- **`MIGRATE_MULTI_ARTIST`** (on) re-files `A, B/Album` or `B, A/Album` under `A/Album` after import.
- **Repair** backs up what it replaces. A verified replacement lets that backup be removed, and a failed refill may put the originals back on its own. If the app or a terminal run is stopped hard while replacements download, the next start puts the originals into the places still empty; a replacement that arrived stays. Any backup still waiting is listed in Settings → Diagnostics.
- **Collection backup**: after a completed Library scan, terminal Library walk or Gap Fill run, a JSON list of every artist, album and track, with the Qobuz ids the app has resolved, goes to `COLLECTION_BACKUP_DIR`. Five dated copies are kept. A backup that comes back empty or much smaller than the last one is held until you confirm it. Upload a backup on Settings to be offered the missing albums it can match on Qobuz; unmatched entries are named in the job log.
