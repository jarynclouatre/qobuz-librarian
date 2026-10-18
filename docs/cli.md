# CLI

[← README](../README.md)

The CLI runs from the same image and Compose service as the web UI, with no separate install: `docker compose run` starts a one-off container from that service that shares the same volumes, config, and run lock. It uses the same matching engine as the web app, walking gaps album by album with yes/no prompts instead of parking a checklist. Run with no arguments for the menu, or flags to jump straight to a mode. The examples below use Docker; from a `pip`/`pipx` or source install run the same commands with `qobuz-librarian` in place of everything up to and including `cli`. Install straight from the repo with the `[lyrics]` extra for the lyrics walk: `pipx install 'qobuz-librarian[lyrics] @ git+https://github.com/jarynclouatre/qobuz-librarian.git'`.

## The run lock

The web app and CLI share one run lock, so only one runs at a time. Free it before a CLI run: switch to terminal mode from **Settings → Mode**, then click **Resume web app** after the CLI run, or stop the web container with `docker compose stop qobuz-librarian` and start it again afterward.

Set `QL_CLI_ONLY=1` to start in terminal mode (the web UI still serves browsing and Settings).

## Interactive menu

```bash
docker compose run --rm -it qobuz-librarian cli
```

Every menu mode has a flag that jumps straight to it; `--help` lists them all. The walks below stay interactive (they confirm per artist or prompt for one), so keep `-it`:

```bash
# The artist walk over every artist, queueing as you go
docker compose run --rm -it qobuz-librarian cli --library-walk

# Gap Fill: fill missing tracks in albums you own (--album-gaps also works)
docker compose run --rm -it qobuz-librarian cli --gap-fill

# Re-download damaged (truncated or corrupt) tracks ('*' at the prompt sweeps everything)
docker compose run --rm -it qobuz-librarian cli --repair
```

The walks remember what you answered, yes or no, and leave it out next time:
the Library walk those artists, and Gap Fill those albums along with the ones
it found complete, could not match, or had nothing on Qobuz to fill from, until
the album's folder gains or loses tracks. Each says how many it left out.
`--reset-walk-seen` brings them all back.

A real Library walk or Gap Fill run refreshes the collection backup after it
scans every artist it set out to. A dry run, a walk you stop or interrupt, an
artist it could not read, or anything left to retry leaves the current backup
unchanged, so the snapshot always describes a walk that finished. A walk you
stop with `s` exits with status 0.

Choose `s` in the menu to open Settings, or use the flag to jump straight in.
It writes to the same store the web Settings page saves to, and `--dry-run`
prints the current values without saving:

```bash
docker compose run --rm -it qobuz-librarian cli --settings
```

## Direct downloads

Both ask before anything downloads, so keep `-it`. Search results carry the web app's marks (**In library**, how many tracks of a part-owned album you have, **Other version in library**). An album URL, or a search whose result you pick by number, shows what is missing and asks `Proceed with downloading N tracks? [y/N]`. `--artist` asks about each incomplete album you own in turn (`y`, `N`, `a` for yes to the rest, `s` to stop) and keeps the albums you approve in the saved queue, so a Ctrl-C leaves them to resume on the next run; it then lists the albums you don't have and asks for the numbers to download. Search in the menu also offers to queue an album; the queue downloads when you leave Search with `q`, and a Ctrl-C drops what is still queued. With no terminal attached, an album download stops at its question without downloading anything, and the run exits with status 1.

```bash
# Download a specific album (URL or "Artist Album" string)
docker compose run --rm -it qobuz-librarian cli https://open.qobuz.com/album/abcd1234

# Work through one artist's catalogue (--include-live, --include-comps and
# --include-singles also list live releases, compilations, and EPs and
# singles; each defaults to its switch in the web Library review)
docker compose run --rm -it qobuz-librarian cli --artist "Paysage d'Hiver"
```

## Common unattended forms

`--upgrade-walk` uses current saved Upgrade results, or checks the library for candidates without changing the saved results.

```bash
# Sweep every artist for quality upgrades, auto-confirming upgrades the scanner can classify safely
docker compose run --rm qobuz-librarian cli --upgrade-walk --auto-safe

# Preview every upgrade candidate without asking
docker compose run --rm qobuz-librarian cli --upgrade-walk --dry-run

# Preview which hi-res library files would downsample to 44.1/48 kHz
docker compose run --rm qobuz-librarian cli --downsample-walk --dry-run

# Fetch lyrics for tracks missing them (--lyrics-synced-only for timed
# lyrics only; --lyrics-rescan to look up tracks checked recently too)
docker compose run --rm qobuz-librarian cli --lyrics-walk

# Start the next walk fresh, revisiting artists already reviewed
docker compose run --rm qobuz-librarian cli --reset-walk-seen

# Full flag reference
docker compose run --rm qobuz-librarian cli --help
```

## What only one side does

| Only in the terminal | Only in the web app |
|---|---|
| The Library walk and Gap Fill walk, which ask as they go | The Library scan and the reviews it builds |
| Downloading without importing (`--no-import`) | New releases checks and Discover |
| | Downloading a single track (Search's Track tab) |
| | Dismissing and bringing back albums |
| | Restoring, removing or deleting kept backups and hi-res originals (Settings > Diagnostics), and restoring from a collection backup |
| | The Settings fields `--settings` leaves out |
| | History entries and `POST_JOB_HOOK` reports for Lyrics and Library migration runs |

## Every flag

`--help` prints the same list with the details.

| Flag | What it does |
|---|---|
| *query* | Search for an album, or download one by Qobuz album URL |
| `--artist NAME` | One artist: fill gaps, then offer albums you're missing |
| `--library-walk` | Every artist in turn, queueing as you go (menu: Library walk) |
| `--gap-fill`, `--album-gaps` | Fill missing tracks in every incomplete album you own (menu: Gap Fill) |
| `--repair` | Re-download damaged (truncated or corrupt) tracks you own |
| `--upgrade-walk` | Review quality upgrades, artist by artist |
| `--downsample-walk` | Bring files above 48 kHz down to 44.1 / 48 kHz in place |
| `--lyrics-walk` | Fetch lyrics for tracks that are missing them |
| `--migrate` | Reorganise an existing library into the Artist/Album layout (menu: Library migration) |
| `--settings` | Set download quality, whether downsampling keeps the originals, and the behaviour toggles |
| `--reset-walk-seen` | Forget which artists and albums the walks have answered, and exit |
| `--no-catalog` | Skip the missing-albums step in Artist mode and the Library walk |
| `--[no-]include-live`, `--[no-]include-comps`, `--[no-]include-singles` | List live releases, compilations, or EPs and singles among missing albums (default: the Library review's switches) |
| `--[no-]include-bonus` | Offer to fill albums whose only missing tracks are other versions of songs they have, in Artist mode, the Library walk and Gap fill (default: the Library review's Bonus versions switch) |
| `--[no-]prefer-hires` | Sort 24-bit and higher sample rate results first (default: the Prefer hi-res setting) |
| `--dry-run` | Show the plan and change nothing; a download a restart cut off is still settled first, as at every start |
| `--force` | Download every track of the album again (search or album URL only) |
| `--yes` | Answer yes to the confirmations; choices still ask |
| `--auto-safe` | With `--upgrade-walk`, confirm only the upgrades it can classify as safe |
| `--no-import` | Download without importing; a complete download stays in staging for the `beet import` command `--help` prints, and other downloads, in the terminal or the web app, go ahead beside it and leave it there |
| `--no-upgrade` | Turn off quality upgrades for this run |
| `--no-downsample` | Skip downsampling new downloads for this run |
| `--[no-]consolidate` | After import, look at sibling folders and offer to merge them |
| `--[no-]migrate-multi-artist` | After import, file 'Primary, Other' albums under 'Primary' |
| `--lyrics-rescan`, `--lyrics-synced-only` | With `--lyrics-walk`: also look up tracks checked in the last 30 days; write only timed lyrics |
| `--migrate-src PATH`, `--migrate-dest PATH`, `--in-place`, `--acoustid` | With `--migrate`: source, destination, move instead of copy, fingerprint untagged files (see [Existing libraries](existing-libraries.md)) |
| `--verbose`, `--quiet` / `-q`, `--no-color` | More detail, warnings and errors only, no colours |
| `--version`, `-h` / `--help` | Print the version or the help |
