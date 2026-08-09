# Contributing

Thanks for taking the time. This file covers filing bugs, suggesting features, and submitting PRs.

## Reporting bugs

Open an issue with:

- What you ran (CLI flags, the URL/album you fed it, or which web page).
- What happened, what you expected.
- A relevant chunk of the log. The fetch log lives at `/data/.qobuz_librarian_log.json` inside the container; the Docker log (`docker compose logs qobuz-librarian`) is also useful.
- Container or pipx install, and your `docker compose version` / Python version.

Do not paste your `password_or_token`. It is in `/config/streamrip/config.toml`; redact it before sharing logs.

## Suggesting a feature

A short issue describing the workflow you want is preferred. The project is focused on **lossless gap-fill + library maintenance**, so features that move it toward a general music app (playlist management, tag editor UI, transcoding to lossy formats, multi-service downloads) are usually out of scope.

## Pull requests

1. Fork, branch from `main`.
2. Install dev deps: `pip install -e ".[test]"`.
3. Add or update tests. They run with `python -m pytest -q` and do not touch the network, beets, or streamrip; external services are mocked.
4. Run `ruff check src tests --fix` before pushing. The `[test]` extras bundle it.
5. If templates or styles changed, run `npm ci && npm run build` and commit the rebuilt `src/qobuz_librarian/web/static/dist/app.css`.
6. CI (`.github/workflows/test.yml`) runs the same checks on push/PR.

## Dev notes

**Building the image locally.** Use `compose.dev.yaml`:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

On M-series Macs add `--platform linux/amd64` if you want the same image as CI (the arm64 path is slower to build and may expose platform-specific surprises):

```bash
docker buildx build --platform linux/amd64 -t qobuz-librarian:dev .
```

**Smoke test.** `scripts/smoke_test.sh` builds the image, boots the container, and checks the web routes respond and the bundled tools are present; no credentials needed (override the test port with `PORT=...`). It verifies that the release image starts and serves expected routes; it is not an end-to-end download test.

**Logo.** The logo artwork lives in `assets/` as `logo-dark.png` and `logo-light.png`; the README serves whichever matches the viewer's colour scheme.

## Behavioural changes

If your change touches how albums are matched, how upgrades are gated, or how files are moved, write a paragraph in the PR describing the old vs new behaviour with at least one concrete example. The match/upgrade/move logic has historically caused the worst real-world surprises and is worth explaining out loud.

## Style

- Black-compatible formatting. Black is not run in CI, but submitted code should follow the same style.
- Comments explain *why*, not *what*. The code can usually speak for itself.
- No new top-level dependencies without a clear maintenance or runtime benefit.

## Releases

Publishing a GitHub release triggers the Docker workflow. Versioning is plain SemVer.
