#!/usr/bin/env bash
# Smoke test: build the image, boot the container, and confirm the web UI
# actually serves. This is NOT an end-to-end download test (that needs real
# Qobuz credentials). It catches "the release is fundamentally broken"
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
TMP_DIR="$(mktemp -d -t qobuz-librarian-smoke.XXXXXX)"
HEALTH_ARGS=(
    --health-interval=1s
    --health-timeout=4s
    --health-start-period=1s
    --health-start-interval=1s
    --health-retries=1
)

cleanup_container() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}

cleanup() {
    cleanup_container
    if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
        rm -rf -- "$TMP_DIR"
    fi
}
trap cleanup EXIT

fail() { echo "FAIL: $1"; exit 1; }

wait_for_server() {
    echo -n "==> Waiting for the web server"
    local up=0
    for _ in $(seq 1 30); do
        if curl -fsS -o /dev/null "${BASE}/healthz" 2>/dev/null; then
            echo " - up"
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
}

wait_for_health() {
    local expected="$1"
    local status=""
    for _ in $(seq 1 15); do
        status=$(docker inspect --format '{{.State.Health.Status}}' "$NAME")
        if [ "$status" = "$expected" ]; then
            echo "  ok  container health -> ${status}"
            return
        fi
        sleep 1
    done
    fail "container health stayed ${status:-unknown}, expected ${expected}"
}

echo "==> Building image"
docker build -t "$IMAGE" .

echo "==> Checking image command dispatch"
if ! bare_help=$(docker run --rm "$IMAGE" --help 2>&1); then
    printf '%s\n' "$bare_help"
    fail "bare --help did not reach the CLI"
fi
if ! printf '%s\n' "$bare_help" | grep -q '^usage: qobuz-librarian'; then
    fail "bare --help did not print CLI usage"
fi
echo "  ok  --help routes to the CLI"

if ! explicit_help=$(docker run --rm "$IMAGE" cli --help 2>&1); then
    printf '%s\n' "$explicit_help"
    fail "cli --help failed"
fi
if ! printf '%s\n' "$explicit_help" | grep -q '^usage: qobuz-librarian'; then
    fail "cli --help did not print CLI usage"
fi
echo "  ok  cli --help still works"

if ! arbitrary=$(docker run --rm "$IMAGE" sh -c 'printf "arbitrary command ok\n"' 2>&1); then
    printf '%s\n' "$arbitrary"
    fail "arbitrary image command failed"
fi
if ! printf '%s\n' "$arbitrary" | grep -qx 'arbitrary command ok'; then
    fail "arbitrary image command did not run"
fi
echo "  ok  arbitrary commands still work"

echo "==> Starting container"
cleanup_container
# WEB_AUTH=none so the smoke test can reach every route directly. With the
# default (auth on and no account yet) a fresh boot correctly sends every page
# to /setup, so the route checks below would only prove the redirect fires, not
# that each template renders. Auth off exercises the handlers themselves.
docker run -d --name "$NAME" "${HEALTH_ARGS[@]}" -e WEB_AUTH=none \
    -p "127.0.0.1:${PORT}:8666" "$IMAGE" >/dev/null

wait_for_server
wait_for_health healthy

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
check /healthz                200
check /readyz                 200
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
    fail "/static/dist/app.css missing or not a real build; the UI would be unstyled"
fi

echo "==> Checking the default login and first-boot path"
cleanup_container
docker run -d --name "$NAME" "${HEALTH_ARGS[@]}" -e DATA_DIR=/data \
    -p "127.0.0.1:${PORT}:8666" "$IMAGE" >/dev/null
wait_for_server
wait_for_health healthy
check /readyz 200

headers="$TMP_DIR/headers"
body="$TMP_DIR/body"
setup_jar="$TMP_DIR/setup-cookies"
login_jar="$TMP_DIR/login-cookies"

code=$(curl -sS -D "$headers" -o "$body" -w '%{http_code}' "${BASE}/")
location=$(sed -n 's/^[Ll]ocation: //p' "$headers" | tr -d '\r' | head -1)
if [ "$code" != "303" ] || [ "$location" != "/setup" ]; then
    fail "fresh root returned ${code} -> ${location:-<none>}, expected 303 -> /setup"
fi
echo "  ok  fresh install redirects to /setup"

curl -fsS -c "$setup_jar" -o "$body" "${BASE}/setup"
csrf=$(sed -n 's/.*name="_csrf_token" value="\([^"]*\)".*/\1/p' "$body" | head -1)
if [ -z "$csrf" ]; then
    fail "setup page did not contain a CSRF token"
fi
code=$(curl -sS -b "$setup_jar" -c "$setup_jar" -o /dev/null \
    -w '%{http_code}' \
    --data-urlencode "_csrf_token=$csrf" \
    --data-urlencode "username=smoke" \
    --data-urlencode "password=smoke-only-password" \
    --data-urlencode "confirm=smoke-only-password" \
    "${BASE}/setup")
if [ "$code" != "303" ]; then
    fail "first-run account setup returned ${code}, expected 303"
fi
echo "  ok  synthetic account created"

for path in /data/.qobuz_web_auth.json /data/.qobuz_web_sessions.json; do
    metadata=$(docker exec "$NAME" stat -c '%a %u:%g' "$path")
    if [ "$metadata" != "600 1000:1000" ]; then
        fail "$path has ${metadata}, expected mode and owner 600 1000:1000"
    fi
done
echo "  ok  credential and session files are private"

code=$(curl -sS -o /dev/null -w '%{http_code}' \
    "${BASE}/api/jobs/nope/status")
if [ "$code" != "401" ]; then
    fail "unauthenticated API returned ${code}, expected 401"
fi
echo "  ok  unauthenticated API is refused"

curl -fsS -c "$login_jar" -o "$body" "${BASE}/login"
csrf=$(sed -n 's/.*name="_csrf_token" value="\([^"]*\)".*/\1/p' "$body" | head -1)
if [ -z "$csrf" ]; then
    fail "login page did not contain a CSRF token"
fi
code=$(curl -sS -b "$login_jar" -c "$login_jar" -o /dev/null \
    -w '%{http_code}' \
    --data-urlencode "_csrf_token=$csrf" \
    --data-urlencode "username=smoke" \
    --data-urlencode "password=smoke-only-password" \
    "${BASE}/login")
if [ "$code" != "303" ]; then
    fail "synthetic login returned ${code}, expected 303"
fi
code=$(curl -sS -b "$login_jar" -o /dev/null -w '%{http_code}' "${BASE}/")
if [ "$code" != "200" ]; then
    fail "authenticated root returned ${code}, expected 200"
fi
echo "  ok  login grants an authenticated session"

docker restart "$NAME" >/dev/null
wait_for_server
wait_for_health healthy
code=$(curl -sS -b "$login_jar" -o /dev/null -w '%{http_code}' "${BASE}/")
if [ "$code" != "200" ]; then
    fail "saved session returned ${code} after restart, expected 200"
fi
echo "  ok  authenticated session survives restart"

echo "==> Checking Docker readiness failure and live recovery"
cleanup_container
bad_data="$TMP_DIR/unreadable-auth"
mkdir -p "$bad_data"
install -m 600 /dev/null "$bad_data/.qobuz_web_auth.json"
bind_uid="$(id -u)"
bind_gid="$(id -g)"
docker run -d --name "$NAME" "${HEALTH_ARGS[@]}" -e DATA_DIR=/data \
    -e PUID="$bind_uid" -e PGID="$bind_gid" \
    -v "$bad_data:/data" -p "127.0.0.1:${PORT}:8666" "$IMAGE" >/dev/null
wait_for_server
check /healthz 200
check /readyz 503
wait_for_health unhealthy
rm -f "$bad_data/.qobuz_web_auth.json"
check /readyz 200
wait_for_health healthy

echo
echo "SMOKE TEST PASSED"
