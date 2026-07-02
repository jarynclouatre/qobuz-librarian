# ── Builder ───────────────────────────────────────────────────────────────────
# Builds a self-contained virtualenv and the Tailwind CSS bundle. `git`,
# build-essential, and the Node toolchain live ONLY here, so they never
# reach the runtime image.
FROM python:3.14-slim AS builder

# git: for the pinned streamrip install (git+https). build-essential: some
# transitive deps compile C extensions if no wheel is available. nodejs/npm:
# Tailwind CLI for the production CSS bundle.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

# Heavy upstream deps first (rarely change → cached above app source).
# streamrip 2.2.0 isn't on PyPI; pin to a known-working dev-branch commit.
# beets is pinned to a verified minor.
# Bump policy: track nathom/streamrip's dev branch, refresh quarterly or
# when a Qobuz-side schema change forces it; verify with scripts/smoke_test.sh
# before changing the SHA. Last verified: 2026-07 against streamrip dev.
#
# streamrip and beets install --no-deps: both cap a few helpers (Pillow,
# aiofiles, tomlkit) far below the versions the librarian runs and verifies, so
# letting them resolve their own trees would either downgrade those or fail the
# build outright. image-lock.txt supplies the whole dependency set instead —
# core beets, the fetchart + inline plugins (beets-default.yaml), the
# chroma/AcoustID path (pyacoustid here, fpcalc via libchromaprint-tools in the
# runtime stage) that beets-chroma.yaml and the migration fingerprint stage use,
# the app's own deps, and every transitive dependency — pinned to a verified
# resolution so a rebuild can't drift to newer releases. Regenerate it with
# scripts/lock-image-deps.sh after changing the streamrip ref or beets pin.
COPY docker/image-lock.txt ./image-lock.txt
RUN pip install --no-cache-dir --no-deps \
        "streamrip @ git+https://github.com/nathom/streamrip.git@e3291615ba6be34aa76df19da8aeb6f41673c6a0" \
 && pip install --no-cache-dir --no-deps "beets==2.12.0" \
 && pip install --no-cache-dir -r image-lock.txt

# App source. image-lock.txt already installed the app's dependencies above, so
# the editable install only links /app/src (--no-deps). The .pth references
# /app/src — kept at the same path in the runtime stage.
COPY LICENSE README.md pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e . --no-deps

# Tailwind production bundle: scans templates + static JS for class names,
# emits a minified CSS file shipped at /static/dist/app.css.
COPY package.json package-lock.json tailwind.config.js ./
RUN npm ci --no-audit --no-fund \
 && ./node_modules/.bin/tailwindcss \
        -i src/qobuz_librarian/web/static/src/app.css \
        -o src/qobuz_librarian/web/static/dist/app.css \
        --minify

# ── Runtime ───────────────────────────────────────────────────────────────────
# No git, no compilers, no pip caches — just the venv + ffmpeg + tiny helpers.
FROM python:3.14-slim AS runtime

LABEL org.opencontainers.image.title="Qobuz Librarian"
LABEL org.opencontainers.image.description="Qobuz downloader + library maintenance (CLI + web UI)"
LABEL org.opencontainers.image.source="https://github.com/jarynclouatre/qobuz-librarian"
# The image REDISTRIBUTES third-party tools under their own licenses (streamrip
# is GPL-3.0-or-later, mutagen GPL-2.0-or-later, plus ffmpeg/flac/beets), so the
# standard machine-readable label reflects the whole image, not just this app.
# This project's OWN source is MIT — see the dedicated label below and LICENSE.
LABEL org.opencontainers.image.licenses="MIT AND GPL-3.0-or-later AND GPL-2.0-or-later"
LABEL com.qobuzlibrarian.app-code-license="MIT"

# ffmpeg: rip/compress (runtime). flac: `flac -t` integrity checks and
# `metaflac` header reads — the reference tools verify frame CRCs and ignore
# embedded cover art, which ffmpeg's decoder does not. gosu: clean PUID/PGID
# drop. procps: ps/top for operators debugging from inside the container.
# libchromaprint-tools: fpcalc, for the optional beets `chroma` (AcoustID)
# plugin used to identify untagged files. curl: POST_JOB_HOOK one-liners
# (ntfy, Discord, …) run inside this container, so it has to be here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        flac \
        gosu \
        procps \
        curl \
        libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Dedicated uid:gid (1000:1000), the default the entrypoint drops to. The
# container starts as root so the entrypoint can chown the config/data volumes
# on first run, then hands off to PUID:PGID (default 1000:1000) via gosu so the
# app never runs as root unless you explicitly ask for it (PUID=0 PGID=0).
RUN groupadd -g 1000 appuser \
 && useradd -u 1000 -g 1000 -m -s /bin/bash appuser

COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv

WORKDIR /app

# Source kept at /app/src so the editable install from the builder resolves.
COPY --chown=appuser:appuser pyproject.toml ./
COPY --chown=appuser:appuser src/ ./src/

# Tailwind CSS bundle built in the builder stage.
COPY --from=builder --chown=appuser:appuser \
     /app/src/qobuz_librarian/web/static/dist/ \
     /app/src/qobuz_librarian/web/static/dist/

# Default config templates (entrypoint seeds them into /config).
COPY --chown=appuser:appuser docker/beets-default.yaml /app/docker/beets-default.yaml
COPY --chown=appuser:appuser docker/beets-chroma.yaml /app/docker/beets-chroma.yaml
COPY --chown=appuser:appuser docker/streamrip-default.toml /app/docker/streamrip-default.toml
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8666

# Explicit marker for the `_in_container()` runtime check in cli.py — Docker
# also creates /.dockerenv, but this also covers Podman/Buildah/rootless.
ENV QL_IN_CONTAINER=1
# entrypoint.sh exports BEETSDIR for the PID-1 process tree, but
# `docker exec ... beet ...` doesn't inherit that. Set it at the image
# level so ad-hoc beets commands inside the container find the library
# without needing -e BEETSDIR=/config/beets every time.
ENV BEETSDIR=/config/beets

# Lets `docker compose ps` / orchestrators detect a wedged container.
# 0.0.0.0 is a bind address, not a destination, so coerce it to 127.0.0.1
# (uvicorn binds to all interfaces in that case, loopback included). A
# user who pins WEB_HOST to a specific interface gets that hostname back.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os; h=os.environ.get('WEB_HOST','0.0.0.0'); h='127.0.0.1' if h=='0.0.0.0' else h; urllib.request.urlopen('http://'+h+':'+os.environ.get('WEB_PORT','8666')+'/healthz', timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]
