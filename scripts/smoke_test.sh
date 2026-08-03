#!/usr/bin/env bash
# Smoke test: build the image, boot the container, and confirm the web UI
# actually serves. This is NOT an end-to-end download test (that needs real
# Qobuz credentials) — it catches "the release is fundamentally broken"
# before you ship: the image builds, the server starts, routes respond,
# and the bundled tools (rip/beet/ffmpeg/flac) are present.
#
# Usage:  ./scripts/smoke_test.sh
# Exits non-zero on the first failure.
set -euo pipefail

# Loopback + high port so this never collides with whatever the user is
# already running on the standard host port. Override with PORT=...
IMAGE="qobuz-librarian:smoke"
NAME="qobuz-librarian-smoke"
PORT="${PORT:-18080}"
BASE="http://127.0.0.1:${PORT}"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Building image"
docker build -t "$IMAGE" .

echo "==> Starting container"
cleanup
# WEB_AUTH=none so the smoke test can reach every route directly. With the
# default (auth on and no account yet) a fresh boot correctly sends every page
# to /setup, so the route checks below would only prove the redirect fires, not
# that each template renders. Auth off exercises the handlers themselves.
docker run -d --name "$NAME" -e WEB_AUTH=none -p "127.0.0.1:${PORT}:8666" "$IMAGE" >/dev/null

echo -n "==> Waiting for the web server"
up=0
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null "${BASE}/" 2>/dev/null; then
        echo " — up"
        up=1
        break
    fi
    echo -n "."
    sleep 1
done
if [ "$up" -ne 1 ]; then
    echo ""
    echo "FAIL: web server didn't respond after 30s. Container logs:"
    docker logs "$NAME" 2>&1 | tail -40
    exit 1
fi

fail() { echo "FAIL: $1"; exit 1; }

check() {
    local path="$1" expect="$2"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE}${path}")
    if [ "$code" != "$expect" ]; then
        fail "${path} returned ${code}, expected ${expect}"
    fi
    echo "  ok  ${path} -> ${code}"
}

echo "==> Checking routes"
check /                       200
check /library                200
check /library/hidden         200
check /upgrade                200   # renders the connect card when Qobuz creds are absent
check /downsample             200
check /repair                 200
check /repair/history         200
check /lyrics                 200
check /migrate                200
check /queue                  200
check /queue/history          200
check /settings               200
check /static/icon.png        200   # favicon + navbar mark
check /static/icon-192.png    200   # PWA icon
check /static/dist/app.css    200   # compiled Tailwind/project CSS; the SW precaches it too
check /api/jobs/nope/status   404   # unknown job id

echo "==> Checking bundled tools in the image"
for bin in rip beet ffmpeg flac metaflac fpcalc; do
    if docker exec "$NAME" sh -c "command -v $bin" >/dev/null 2>&1; then
        echo "  ok  $bin present"
    else
        fail "$bin missing from image"
    fi
done

echo "==> Checking branding"
# Host-side curl, same as the route checks above.
if curl -fsS "${BASE}/" 2>/dev/null | grep -q "Qobuz Librarian"; then
    echo "  ok  page shows 'Qobuz Librarian'"
else
    fail "branding 'Qobuz Librarian' not found in served HTML"
fi

echo "==> Checking compiled CSS is a real build"
# A 200 alone isn't enough: the HTML routes render even with no stylesheet, so a
# regression in the Docker CSS build/copy step would otherwise pass smoke with an
# unstyled UI. Assert the asset is substantial and carries a real Tailwind token.
css=$(curl -fsS "${BASE}/static/dist/app.css" 2>/dev/null || true)
if [ "$(printf '%s' "$css" | wc -c)" -gt 1000 ] && printf '%s' "$css" | grep -q -- '--tw-'; then
    echo "  ok  app.css served and looks like a real Tailwind build"
else
    fail "/static/dist/app.css missing or not a real build — the UI would be unstyled"
fi

echo
echo "SMOKE TEST PASSED"
