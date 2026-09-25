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

# Fill the gaps in albums you own (an album missing at least 70% of its
# tracks, and at least 4, is refetched whole and the tracks you have are
# replaced)
docker compose run --rm -it qobuz-librarian cli --album-gaps

# Re-download damaged (truncated) tracks ('*' at the prompt sweeps everything)
docker compose run --rm -it qobuz-librarian cli --repair
```

A real Library walk or Album gaps run refreshes the collection backup after it
scans every artist it set out to. A dry run, an interrupted walk, an artist it
could not read, or anything left to retry leaves the current backup unchanged,
so the snapshot always describes a walk that finished.

Choose `s` in the menu to open Settings, or use the flag to jump straight in.
It writes to the same store the web Settings page saves to, and `--dry-run`
prints the current values without saving:

```bash
docker compose run --rm -it qobuz-librarian cli --settings
```

## Direct downloads

Both confirm each album before anything downloads (download, queue, or skip), so keep `-it`. With no terminal attached, an album download stops at its confirmation without downloading anything, and the run exits with status 1.

```bash
# Download a specific album (URL or "Artist Album" string)
docker compose run --rm -it qobuz-librarian cli https://open.qobuz.com/album/abcd1234

# Work through one artist's catalogue (--include-singles and/or
# --include-comps to also offer singles and compilation appearances)
docker compose run --rm -it qobuz-librarian cli --artist "Paysage d'Hiver"
```

## Common unattended forms

`--upgrade-walk` uses current saved Upgrade results, or checks the library for candidates without changing the saved results.

```bash
# Sweep every artist for quality upgrades, auto-confirming upgrades the scanner can classify safely
docker compose run --rm qobuz-librarian cli --upgrade-walk --auto-safe

# Preview which hi-res library files would downsample to 44.1/48 kHz (changes nothing)
docker compose run --rm qobuz-librarian cli --downsample-walk --dry-run

# Fetch lyrics for tracks missing them (--lyrics-synced-only for timed
# lyrics only; --lyrics-rescan to re-query tracks already checked)
docker compose run --rm qobuz-librarian cli --lyrics-walk

# Start the next walk fresh, revisiting artists already reviewed
docker compose run --rm qobuz-librarian cli --reset-walk-seen

# Full flag reference
docker compose run --rm qobuz-librarian cli --help
```

## What only one side does

| Only in the terminal | Only in the web app |
|---|---|
| The Library walk and Album gaps walk, which ask as they go | The Library scan and the reviews it builds |
| The `--include-singles` and `--include-comps` switches | New releases checks and Discover |
| Downloading without importing (`--no-import`) | Dismissing and bringing back albums |
| | Putting downsampled originals back, and restoring from a collection backup |
| | The Settings fields `--settings` leaves out |
