<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.png">
    <img src="assets/logo-light.png" alt="Qobuz Librarian" width="520">
  </picture>
</p>

<p align="center"><em>Build and maintain a complete, lossless library from Qobuz.</em></p>

<p align="center">
  <a href="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/test.yml"><img src="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/docker.yml"><img src="https://github.com/jarynclouatre/qobuz-librarian/actions/workflows/docker.yml/badge.svg" alt="Docker"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
</p>

Qobuz Librarian downloads music from Qobuz and files it with [beets](https://beets.io/). Point it at your music folder and it works out what you're missing: albums by artists you already have, tracks missing from albums you own, better-quality versions, damaged files and lyrics.

It has a web UI and a CLI.

<p align="center">
  <img src="assets/screenshot-search.png" alt="Search results in the web UI" width="800">
</p>

## Features

- **Missing albums.** A Library scan lists albums you don't have by artists you do. Live releases, compilations, EPs and singles, and what Qobuz files under other (mostly label reissues) can be switched on in the review.
- **Gap Fill.** Finds albums you own that are missing tracks and downloads just those tracks, from the same edition you have.
- **Editions.** Search marks the editions you own as **In library** by matching recording IDs; remasters and different mixes count as separate releases. [How matching works](docs/existing-libraries.md#how-editions-are-matched).
- **Single tracks.** Switch Search to **Track** to download one song into its album's folder.
- **Upgrades.** Finds albums Qobuz now has in higher quality and replaces them, keeping a backup until the new files check out.
- **Downsample.** Converts FLACs above 48 kHz to 44.1 or 48 kHz, on demand or for every new download.
- **New releases.** Checks regularly for new albums by artists in your library.
- **Discover.** With a free Last.fm API key, suggests artists like yours, browses your library by genre and lists albums you saved on Qobuz that aren't on disk yet.
- **Lyrics.** Fetched on import, with a Lyrics page to fill in the rest of your library.
- **Repair.** Finds truncated or corrupt FLACs and replaces just those tracks when Qobuz has the same recording, or offers the whole album again when it can't tell.
- **Migration.** Reorganises an existing library into `Artist/Album (Year)` folders, copying by default.
- **Collection backup.** After each Library scan it saves a list of everything you own. Upload one later and it offers to download what's missing.
- **Survives restarts.** Queues and reviews come back after a restart. A Search download cut off part way picks up the tracks it was missing, and a Library or Upgrade run puts the albums it had not finished back in its review, ticked.

Scans give you a list to tick through before anything is downloaded or changed. Search and Lyrics act right away.

Downloads use the best quality Qobuz has for each release, from CD up to 24-bit/192 kHz, or a lower tier set on Settings.

## Quick start (Docker)

```bash
mkdir qobuz-librarian && cd qobuz-librarian
curl -O https://raw.githubusercontent.com/jarynclouatre/qobuz-librarian/main/compose.yaml
curl -O https://raw.githubusercontent.com/jarynclouatre/qobuz-librarian/main/.env.example
cp .env.example .env
# edit .env: at minimum, point QL_MUSIC_DIR at your music folder, and set
# PUID/PGID if `id -u` / `id -g` are not 1000
mkdir -p music   # skip if QL_MUSIC_DIR points at an existing folder
docker compose up -d
```

Open <http://localhost:8666> and choose a username and password. On **Settings**, paste your Qobuz token and your email or user ID (see below).

`QL_MUSIC_DIR` should hold only music: the app moves and replaces files inside it.

The image is [`dinkeyes/qobuz-librarian`](https://hub.docker.com/r/dinkeyes/qobuz-librarian) on Docker Hub and bundles streamrip, beets, ffmpeg and the FLAC tools. On Windows, run the commands in WSL or Git Bash. The image includes the CLI too, and it can also be installed with pipx; see [CLI](docs/cli.md).

To update, run `docker compose pull` and then `docker compose up -d` (`restart` keeps the old image). Compare your `compose.yaml` with the latest one after updating.

### Your Qobuz token

You need a paid Qobuz account. The app signs in with a token, not your password.

To find it, sign in to the [Qobuz web player](https://play.qobuz.com), open dev tools (F12) and find Local Storage for `play.qobuz.com` (the **Application** tab in Chrome and Edge, **Storage** in Firefox). Open the `localuser` entry and copy its `token` value; the `id` next to it is your user ID. If you already use streamrip with `use_auth_token = true`, the token is the `password_or_token` value in its `config.toml`.

Searching and scanning need only the token. Downloading also needs your email or user ID.

## Documentation

- **[Configuration](docs/configuration.md)**: environment variables, download quality, beets and streamrip config, NAS permissions, reverse proxies, and what the app does on its own.
- **[Existing libraries](docs/existing-libraries.md)**: the folder layout it expects, migrating, bringing your own beets database, and the first big scan.
- **[CLI](docs/cli.md)**: most of the same features from the terminal, including installing without Docker.
- **[JSON API](docs/api.md)**: read-only job state for dashboards and scripts.
- **[Troubleshooting](docs/troubleshooting.md)**: common errors and what to check.

## Security

The web UI asks for a login, with failed sign-ins rate-limited. To set the login before the first visit, put `WEB_AUTH_USER` and `WEB_AUTH_PASSWORD` in `.env`; to keep the port off your network, set `WEB_BIND=127.0.0.1`. Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address. The container runs as `PUID:PGID`, not root.

For access from outside your network, use a VPN, Tailscale or an authenticating reverse proxy. More in [SECURITY.md](SECURITY.md).

## Limitations

- Qobuz only, and the library stays FLAC.
- Keep the staging folder on a local disk. On network shares and unusual disks, anything the disk can't do safely is refused up front, and Settings → Diagnostics shows what each folder supports.
- Stop the app before syncing, restoring or replacing the beets database.
- One stack per library.
- Matching works best with Latin-script names. Other scripts work, but edition labels like "Deluxe" are recognised in English.
- Installing the web UI as an app needs HTTPS (or `localhost`).

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate      # PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[test]"
python -m pytest -q
```

To build the Docker image from a checkout:

```bash
git clone https://github.com/jarynclouatre/qobuz-librarian.git
cd qobuz-librarian
cp .env.example .env
mkdir -p music   # skip if QL_MUSIC_DIR points at an existing folder
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

The wheel includes the compiled stylesheet. When changing templates or styles in a source checkout, rebuild it with `npm ci && npm run build`; the image build does this too. `ruff check src tests` runs in CI. See [CONTRIBUTING.md](CONTRIBUTING.md) for the rest.

## Acknowledgements

Qobuz Librarian is built on these projects, all bundled in the Docker image:

- **[streamrip](https://github.com/nathom/streamrip)** (nathom): the Qobuz downloader. GPL-3.0.
- **[beets](https://beets.io/)**: tagging, cover art, library organisation. MIT.
- **[mutagen](https://github.com/quodlibet/mutagen)**: audio metadata reading/writing. GPL-2.0-or-later.
- **[FFmpeg](https://ffmpeg.org/)**: audio probing and transcoding. LGPL/GPL depending on build.
- **[FLAC](https://xiph.org/flac/)** (Xiph.Org): integrity verification and header reads. BSD.

Lyrics come from [LRCLIB](https://lrclib.net/) and via [syncedlyrics](https://github.com/moehmeni/syncedlyrics) (NetEase, Megalobiz, Musixmatch, Genius). The web UI uses [FastAPI](https://fastapi.tiangolo.com/), [htmx](https://htmx.org/), and [Tailwind CSS](https://tailwindcss.com/) with Qobuz Librarian's own `ql-*` component layer. Thanks to all their maintainers.

## License

MIT; see [LICENSE](LICENSE). The Docker image includes the tools above under their own licences: streamrip (GPL-3.0) runs as a separate program and mutagen (GPL-2.0-or-later) is imported as a library.
