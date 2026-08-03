# CLI

[← README](../README.md)

The CLI runs from the same image and Compose service as the web UI, with no separate install: `docker compose run` starts a one-off container from that service that shares the same volumes, config, and download lock. It uses the same matching engine as the web app, walking gaps album by album with yes/no prompts instead of parking a checklist. Run with no arguments for the menu, or flags for unattended runs. The examples below use Docker; from a `pip`/`pipx` or source install run the same commands as `qobuz-librarian …` (drop the `docker compose run --rm` prefix). Install straight from the repo with the `[lyrics]` extra for the lyrics walk: `pipx install 'qobuz-librarian[lyrics] @ git+https://github.com/jarynclouatre/qobuz-librarian.git'`.

## The download lock

The web app and CLI share one download lock, so only one runs at a time. Free it before a CLI run: switch to terminal mode from **Settings → Mode**, then click **Resume web app** after the CLI run, or stop the web container with `docker compose stop qobuz-librarian` and start it again afterward.

Set `QL_CLI_ONLY=1` to start in terminal mode (the web UI still serves browsing and Settings).

## Interactive menu

```bash
docker compose run --rm -it qobuz-librarian cli
```

Three menu modes also have flags that jump straight to them — still interactive (they confirm per artist or prompt for one), so keep `-it`:

```bash
# The artist walk over every artist, queueing as you go
docker compose run --rm -it qobuz-librarian cli --library-walk

# Fill missing tracks in incomplete albums you own, nothing else
docker compose run --rm -it qobuz-librarian cli --album-gaps

# Re-download damaged (truncated) tracks ('*' at the prompt sweeps everything)
docker compose run --rm -it qobuz-librarian cli --repair
```

## Common unattended forms

```bash
# Download a specific album (URL or "Artist Album" string)
docker compose run --rm qobuz-librarian cli https://open.qobuz.com/album/abcd1234

# Work through one artist's catalogue (--include-singles and/or
# --include-comps to also offer singles and compilation appearances)
docker compose run --rm qobuz-librarian cli --artist "Paysage d'Hiver"

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

The CLI honours the same `.env` and `compose.yaml` settings as the web UI.
