# Configuration

[← README](../README.md)

Host paths and the web port come from `.env`, which is gitignored. Start by copying `.env.example`.

Docker Compose reads those values on the host and maps them to the container paths used by the app. Compose only passes a variable into the container when `compose.yaml` lists it under `environment:`.

Use the **Settings** page for day-to-day behaviour:

- download quality
- lyrics and artwork
- beets folder layout
- scan cadence

These settings apply to new jobs. A field you change on the Settings page keeps that value across restarts and wins over `.env` for that field; fields you never change there keep following `.env`. Lower-level options stay in `.env` or `compose.yaml` and may need a restart, including timeouts, worker counts, cache and pacing tunables, `WEB_AUTH*`, host bind, and `LOG_LEVEL`.

## Host paths

| Variable | Default | Purpose |
|---|---|---|
| `QL_MUSIC_DIR` | `./music` | Music library; beets imports into this |
| `QL_STAGING_DIR` | `./staging` | Scratch space for in-progress downloads |
| `QL_UPGRADE_BACKUPS` | `./upgrade_backups` | Backups taken before a quality upgrade |
| `QL_COLLECTION_BACKUPS` | `./collection_backups` | Collection snapshots used by Backup & Restore |
| `WEB_PORT` | `8666` | Host port for the web UI |

The music, staging, and upgrade-backup directories must be separate,
non-nested trees. The app refuses to start if any one is the same as, inside,
or an alias of another. This keeps in-progress downloads and retained backups
out of library scans and import targets.

## Behaviour toggles

| Variable | Default | Purpose |
|---|---|---|
| `STREAMRIP_QUALITY` | `4` | Download tier 2–4 (see [Download quality](#download-quality)) |
| `PREFER_HIRES` | `true` | When a release has several versions, pick the hi-res master rather than the original edition |
| `LYRICS_ENABLED` | `true` | Fetch lyrics on import |
| `LYRICS_FORMAT` | `embed` | `embed` (FLAC tag), `sidecar` (.lrc), or `both` |
| `LYRICS_PROVIDERS` | unset | Ordered comma list; unset tries the bundled provider library's own order |
| `LYRICS_PROVIDER_TIMEOUT` | `30` | Maximum seconds for one provider lookup before its circuit-breaker records a failure |
| `ARTWORK` | `sidecar` | Cover art: `sidecar`, `embed`, or `both` |
| `COLLECTION_BACKUP_DIR` | `/collection_backups` in Compose; app data otherwise | Container path where the collection backup is written after each library scan |
| `LASTFM_API_KEY` | unset | Enables the Discover tab; without it the tab does not appear. Also settable on the Settings page, and `LASTFM_API_KEY_FILE` reads it from a file |
| `AUTO_LIBRARY_SCAN` | `true` | Offer the one-time baseline scan on the Search page on first run, and auto-resume an interrupted library scan when the app is idle (`false` turns both off; the manual Resume button still works) |
| `NEW_RELEASE_CHECK_INTERVAL` | `86400` | How often (seconds) to auto-check for new releases; daily (also on Settings) |
| `ARTIST_CATALOG_CACHE_TTL` | `604800` | How long (seconds) artist album-lists stay cached; 7 days |
| `REPAIR_CACHE_ENABLED` | `true` | Cache the repair scan's Qobuz ISRC lookups (files are still decode-tested fresh every scan) |
| `REPAIR_CACHE_TTL_DAYS` | `30` | How long a cached ISRC lookup is reused before re-verifying (`0` = keep until the db is deleted) |
| `UPGRADE_SCAN_ENABLED` | `true` | Show Upgrade and include the quality-upgrade pass in Library scans (`false` hides Upgrade and skips that pass) |
| `AUTO_UPGRADE_ENABLED` | `false` | Let an ordinary CLI download or gap-fill run also upgrade an album Qobuz can now serve better, after confirmation. Ordinary web downloads keep the selected edition; use the Upgrade page to review and approve web replacements. |
| `DOWNSAMPLE_HIRES_ENABLED` | `false` | Downsample hi-res FLACs as they download (see below) |
| `UPGRADE_SINGLES_ENABLED` | `false` | Let the Upgrade walk re-rip tracks you pulled as singles |
| `MIGRATE_MULTI_ARTIST` | `false` | Re-file `A, B/Album` under `A/Album` after import |
| `SUPPRESS_SINGLE_TRACK_GAPS` | `false` | Hide the rest of an album from gap scans once you download one track from it |
| `CONSOLIDATE` | `false` | Merge sibling/duplicate album folders (CLI-only) |

`MIGRATE_MULTI_ARTIST` affects new imports only. It never replaces a name that already exists under the primary artist; conflicting files stay in their original folder for review. If Beets splits a gap-filled album between the combined and primary artist folders, Qobuz Librarian safely reunites only the non-conflicting files regardless of this preference. Both operations resume or roll back after a restart before other library work begins.

`DOWNSAMPLE_HIRES_ENABLED` only touches new downloads (88.2 / 176.4 / 352.8 kHz → 44.1; 96 / 192 / 384 → 48; bit depth preserved, originals replaced atomically). To downsample hi-res already in your library, use the on-demand **Downsample** mode instead.

Pacing and timeout knobs are forwarded in `compose.yaml`, so setting them in `.env` takes effect:

- `RIP_TIMEOUT`
- `DELAY_BETWEEN`
- `ARTIST_API_DELAY`
- `ARTIST_SCAN_WORKERS`
- `MISSING_ALBUMS_MIN_TRACKS`

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

To keep hi-res masters smaller, pull tier `4` and either enable import-time downsampling (`DOWNSAMPLE_HIRES_ENABLED`) or run **Downsample** on demand.

## beets & streamrip config

The bundled tools' full config files live in the persistent `config` volume, seeded once and never overwritten:

- `…/beets/config.yaml`: tagging, paths, plugins ([beets docs](https://beets.readthedocs.io/))
- `…/streamrip/config.toml`: downloader settings ([streamrip docs](https://github.com/nathom/streamrip))

The default Compose `/config` named volume is supported for the beets database. If you replace it or set `BEETS_DB_PATH`, run the app on Linux with `/proc/self/fd` and keep the database on a local filesystem that supports hard links, xattrs, advisory `flock`, file leases, atomic `renameat2` exchange, and file and directory `fsync`. NFS, SMB/CIFS, and other network filesystems are not supported for the database; the music library itself may still live on network storage.

Set folder and file naming with `BEETS_PATH_DEFAULT`, `BEETS_PATH_SINGLETON`, and `BEETS_PATH_COMP` on the **Settings** page or in `.env`. These use beets path syntax, for example `$albumartist/$album ($year)/$track - $title`. Enter that raw template on the Settings page. In `.env`, single-quote the whole assignment so Compose does not consume the beets variables:

```dotenv
BEETS_PATH_DEFAULT='$albumartist/$album ($year)/$track - $title'
```

Set the plugins you choose with `BEETS_PLUGINS`. When set, this list replaces the plugins selected in `config.yaml` for imports run by Qobuz Librarian. The app then adds `inline` for its multi-disc folder field, the artwork plugins required by `ARTWORK`, and its internal import guards. Plugins that need their own config block, such as a lastgenre API key or replaygain backend, still require an edit to `config.yaml`.

For a `pip` or `pipx` installation, install beets 2.13.1 in the same environment. Qobuz Librarian normally finds that environment from the `beet` launcher. If the launcher is an unusual wrapper, set `BEETS_PYTHON` to the absolute path of the Python executable in that environment. Other beets versions are refused because the import and recovery contract is verified against 2.13.1.

Treat enabled beets plugins as trusted code. A plugin must finish all database work before its `beet` command exits; detached or background database writers are unsupported. Stop Qobuz Librarian completely before running a manual `beet` command, since external commands do not participate in the app's database coordination.

For its own imports, the downloader pins five beets settings regardless of your config:

- `autotag: no` keeps Qobuz's tags
- `write: no` prevents beets from rewriting media tags during the move
- `move: yes` clears staging, including across filesystems
- `incremental: no` rescans on retry
- `duplicate_action: merge` gap-fills into the existing folder without deleting your files

Your own `beet` commands read your config unchanged. Edits apply on the next import, no restart.

## Permissions (NAS and shared storage)

The container runs as `PUID:PGID` (default `1000:1000`), so downloads are owned by a normal user rather than root. If your host or media share owner is not `1000`, set them in `.env` and they flow straight in:

```bash
PUID=1000   # id -u
PGID=1000   # id -g
```

On boot, the container chowns its managed config folders and the `data`, `staging`, `upgrade_backups`, and `collection_backups` volumes to that user, and warns if a mounted path is not writable. `/music` is left alone because it is often a large NAS mount; the run user must be able to write to it.

Operations that replace or remove an existing library file also require the share to flush file and directory changes reliably. If the mount cannot do that, Qobuz Librarian stops the destructive step and keeps the original.

For a read-only music share, append `:ro` to the `/music` bind and set `QL_CHECK_VOLUMES=0` in `.env`; otherwise write endpoints, including scan starts, return 503 while the write check fails. The check is live, so fixing the mount or its ownership takes effect without a restart.

If the bind dirs were auto-created as root on a first `up`, chown them:

```bash
sudo chown -R 1000:1000 ./music ./staging ./upgrade_backups ./collection_backups
```

To run as root, set `PUID=0` and `PGID=0` explicitly. A non-numeric typo makes the container refuse to start rather than silently falling back to root.

## Timezone

Set `TZ` in `.env` (an IANA name like `America/Edmonton`) so exact timestamps in History and on job pages show your local time instead of UTC. Relative labels like "2 hr ago" are correct either way.

## Deployment

**Login.** The web UI requires sign-in out of the box. The password is stored as a salted PBKDF2 hash (`0600`, never plaintext) and the session is an `HttpOnly` cookie backed by a persisted token digest. If that session cannot be saved, sign-in returns a clear 503 instead of issuing a cookie that will stop working after restart. New and reset passwords need at least 15 characters, and common or product-name passwords are refused; existing credentials keep working after an upgrade. Set the credentials on the first-visit screen, or seed `WEB_AUTH_USER` / `WEB_AUTH_PASSWORD` in `.env` so the box comes up already locked down. Those two double as a password reset: change them and restart. The same policy applies when the password comes from `WEB_AUTH_PASSWORD_FILE`; a changed invalid seed stops startup without replacing the saved login. To reset without env vars, stop the container, delete `.qobuz_web_auth.json` from the data volume, and set new credentials on the next visit.

`WEB_AUTH=none` disables login. Use it only on a trusted LAN or behind your own authenticating proxy. The container logs a warning every boot while login is off.

**Container probes.** `/healthz` is a cheap process-liveness check. Docker uses `/readyz`, which returns 503 when an existing login cannot be read, the data directory or Queue/History database is unavailable, the single-writer lock is unsafe or lost, or shutdown has started. Deliberate write pauses such as terminal mode, another active run, recovery, or read-only music and staging volumes return 200 with `status: degraded`; this keeps the usable read-only interface and Diagnostics available instead of inviting a restart loop. Both routes are available without signing in and return only category names, never paths or credentials.

**Behind a reverse proxy.** Set `FORWARDED_ALLOW_IPS` to the proxy's address so the failed-login throttle counts attempts per real client, not per the shared proxy IP. Point it at your proxy, not `*`. The default, `127.0.0.1`, does not cover a proxy on a Docker network, so leaving it unset there throttles every visitor as one; the log names the untrusted peer so you can verify that it really is your proxy before adding it.

**Keeping the token out of the environment.** `docker inspect` exposes environment variables, so to keep the Qobuz token out of them, point `QOBUZ_USER_AUTH_TOKEN_FILE` at a file containing only the token (a [Docker secret](https://docs.docker.com/engine/swarm/secrets/) or read-only bind mount) instead of setting `QOBUZ_USER_AUTH_TOKEN`. The web-login password supports the same pattern with `WEB_AUTH_PASSWORD_FILE`.

**Hardening.** The bundled `compose.yaml` ships with `mem_limit: 1g`, `pids_limit: 256`, `no-new-privileges`, `cap_drop: [ALL]`, and `0600` token files. It adds back only the capabilities needed for the PUID/PGID handover. The built-in login is a single shared credential with brute-force limiting (a 429 after 5 failures an hour from one IP, or 10 against the same username from anywhere, so rotating source addresses does not defeat it, clearing 15 minutes after the last failure), which is appropriate for a trusted network; use a proxy, VPN, or Tailscale for internet exposure. The image is multi-arch (`linux/amd64`, `linux/arm64`), so arm64 NAS boxes run natively, and a `--read-only` rootfs works with `--tmpfs /tmp`. For a `/var/tmp` mount instead, set `APP_HOME=/var/tmp` in `.env` and use `--tmpfs /var/tmp`. See [SECURITY.md](../SECURITY.md).

## Notifications

`POST_JOB_HOOK` runs a command of your choice every time a background job finishes: downloads, scans, repairs, all of it. The job's final state arrives as JSON on stdin (`id`, `status`, `title`, `edition`, `display_title`, `artist`, `error`, `finished_at`), and `POST_JOB_HOOK_TIMEOUT` (default 10s) caps a slow endpoint. The command runs inside the container, which bundles `curl` and Python for exactly this. A command that cannot start, times out, or exits nonzero is recorded in the application log without exposing the command or its stderr.

The hook must not run `beet` or otherwise edit its database while Qobuz Librarian is running.

The simplest form pushes the raw JSON to an [ntfy](https://ntfy.sh) topic, straight from `.env`:

```bash
POST_JOB_HOOK=curl -s -o /dev/null -H "Title: Qobuz Librarian" -d @- https://ntfy.sh/your-topic
```

For a readable message, point the hook at a small script in the config volume instead (`POST_JOB_HOOK=/config/notify.sh`, made executable with `chmod +x`):

```sh
#!/bin/sh
# ntfy, one line per finished job: "done: Bonobo - Black Sands"
msg=$(python3 -c 'import json,sys
j = json.load(sys.stdin)
line = j["status"] + ": " + " - ".join(x for x in (j.get("artist"), j.get("title")) if x)
print(line + (" (" + j["error"] + ")" if j.get("error") else ""))')
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

The hook also fires once if the saved Qobuz token stops being accepted (`status` is `auth_lost`), so an unattended box tells you the moment downloads would start failing instead of leaving it for you to discover.

## What the app does on its own

On first run the Search page *offers* a one-time baseline scan (`AUTO_LIBRARY_SCAN`) rather than starting it for you. Once that baseline exists, periodic new-release checks (`NEW_RELEASE_CHECK_INTERVAL`) run on their own, read-only, on a background timer, so they keep to the interval even when nobody has the app open. Both park a review list; nothing is downloaded or changed until you act on it.

- **Library gap-fill** can add missing albums or missing tracks after review; it does not overwrite existing tracks.
- **After a download**, it re-checks the new album's track lengths against Qobuz and flags **Repair** if one is short. Read-only (a clean truncation can still decode).
- **Upgrade** and **Downsample** change files only when you start them. Upgrade backs up the originals first (`UPGRADE_BACKUP_RETENTION_DAYS`); Downsample rewrites in place after verifying each file decodes, or, with *Keep originals when downsampling* set to keep (`DOWNSAMPLE_KEEP_ORIGINALS`; you're asked to choose keep or delete on your first downsample), parks the hi-res copies in the backup area first so the rewrite can be undone from Settings → Diagnostics until the retention window ends.
- **Lyrics** writes tags or `.lrc` sidecars, not the audio.
- **Consolidation** (`CONSOLIDATE`, off) merges duplicate folders, CLI-only (it needs per-folder confirmation).
- **`MIGRATE_MULTI_ARTIST`** (off) re-files `A, B/Album` under `A/Album` after import.
