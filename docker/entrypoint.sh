#!/bin/bash
set -e

CONFIG_DIR="${CONFIG_DIR:-/config}"
case "$CONFIG_DIR" in
    /*) ;;
    *)
        echo "[fatal] CONFIG_DIR must be an absolute path (got ${CONFIG_DIR})." >&2
        exit 1
        ;;
esac
if ! CONFIG_DIR="$(realpath -m -- "$CONFIG_DIR")" || [ "$CONFIG_DIR" = "/" ]; then
    echo "[fatal] CONFIG_DIR must resolve to a dedicated directory, not /." >&2
    exit 1
fi
BEETS_DIR="$CONFIG_DIR/beets"
STREAMRIP_DIR="$CONFIG_DIR/streamrip"

# ── First-run bootstrap ───────────────────────────────────────────────────────
# mkdir is a no-op when they exist. With --read-only and a missing mount,
# `mkdir -p` would fail under `set -e`; warn instead so the writability
# diagnostics below can run and surface the real problem to the user.
mkdir -p "$BEETS_DIR" "$STREAMRIP_DIR" 2>/dev/null || \
    echo "[warn] couldn't create $CONFIG_DIR subdirs. Mount /config read-write." >&2
for d in /staging /music /data /upgrade_backups /collection_backups; do
    [ -d "$d" ] || mkdir -p "$d" 2>/dev/null || \
        echo "[warn] couldn't create $d. Add a mount, or remove --read-only on the rootfs." >&2
done

if [ ! -f "$BEETS_DIR/config.yaml" ]; then
    echo "[init] Creating default beets config at $BEETS_DIR/config.yaml"
    cp /app/docker/beets-default.yaml "$BEETS_DIR/config.yaml" 2>/dev/null || \
        echo "[warn] couldn't seed beets config. Mount /config read-write." >&2
fi

if [ ! -f "$STREAMRIP_DIR/config.toml" ]; then
    echo "[init] Creating default streamrip config at $STREAMRIP_DIR/config.toml"
    cp /app/docker/streamrip-default.toml "$STREAMRIP_DIR/config.toml" 2>/dev/null || \
        echo "[warn] couldn't seed streamrip config. Mount /config read-write." >&2
fi

# Enforce the streamrip settings the librarian depends on, every boot. These
# aren't user-tunable (there's no UI for them) and the /config volume persists
# across image rebuilds, so a config written by an older build is brought into
# line rather than left stale. Best-effort: a read-only /config leaves the
# value alone and the writability check below explains why.
#   downloads_enabled=false: its downloads.db otherwise blocks re-download of
#     any track the user removed by hand. The ^anchor in the key match leaves
#     failed_downloads_enabled untouched.
#   add_singles_to_folder=true: per-track gap-fill must land each track in its
#     own folder, or beets routes multiple albums into one on-disk folder.
#   download_booklets=false: the librarian imports audio only; a fetched
#     booklet PDF never lands in the library, just clutters /staging and
#     inflates the leftover count that can abort a --yes run.
#   check_for_updates=false: streamrip is pinned into the image, so its
#     "a new version is available" notice can't be acted on; turning it off
#     also drops a PyPI request from every download.
reconcile_streamrip() {
    python3 - "$STREAMRIP_DIR/config.toml" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
managed = {
    "database": ("downloads_enabled", "false"),
    "filepaths": ("add_singles_to_folder", "true"),
    "qobuz": ("download_booklets", "false"),
    "misc": ("check_for_updates", "false"),
}
lines = path.read_text().splitlines(keepends=True)
found_sections = set()
found_keys = set()
result = []
section = None

for line in lines:
    header = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line.rstrip("\r\n"))
    if header:
        section = header.group(1).strip()
        if section in managed:
            found_sections.add(section)
        result.append(line)
        continue

    if section in managed:
        key, want = managed[section]
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            marker = (section, key)
            if marker not in found_keys:
                result.append(f"{key} = {want}\n")
                found_keys.add(marker)
            # Drop later copies of a managed key. An older entrypoint rewrote
            # every copy but retained duplicates, producing invalid TOML.
            continue
    result.append(line)

for target_section in managed:
    if target_section not in found_sections:
        print(
            f"[warn] streamrip config has no [{target_section}]; "
            "managed settings could not be enforced.",
            file=sys.stderr,
        )

# A key absent from an older config belongs inside its own existing table,
# never at EOF under whichever table happens to be last.
for target_section, (key, want) in managed.items():
    marker = (target_section, key)
    if target_section not in found_sections or marker in found_keys:
        continue
    for index, line in enumerate(result):
        header = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line.rstrip("\r\n"))
        if header and header.group(1).strip() == target_section:
            result.insert(index + 1, f"{key} = {want}\n")
            found_keys.add(marker)
            break

rewritten = "".join(result)
if rewritten == "".join(lines):
    raise SystemExit(0)

mode = stat.S_IMODE(path.stat().st_mode)
tmp_name = None
try:
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as tmp:
        tmp_name = tmp.name
        tmp.write(rewritten)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.chmod(tmp_name, mode)
    os.replace(tmp_name, path)
finally:
    if tmp_name is not None:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass

print("[init] Reconciled managed streamrip settings.")
PY
}
if ! reconcile_streamrip; then
    echo "[warn] couldn't reconcile managed streamrip settings. Mount /config read-write." >&2
fi

enforce_streamrip() {
    local key="$1" want="$2" section="$3" cfg="$STREAMRIP_DIR/config.toml" cur
    # Match the key with flexible whitespace around '=' so a hand-edited
    # 'downloads_enabled=false' (or extra spaces) is found and rewritten in
    # place. Demanding exactly '^key = ' would miss it, fall through to the
    # section-append branch below, and insert a duplicate key, which makes the
    # TOML unparseable and breaks every download. The '^' anchor still spares
    # longer keys like failed_downloads_enabled.
    cur=$(sed -n "s/^${key}[[:space:]]*=[[:space:]]*\(.*\)/\1/p" "$cfg" 2>/dev/null)
    if [ "$cur" = "$want" ]; then
        return 0
    fi
    if grep -q "^${key}[[:space:]]*=" "$cfg" 2>/dev/null; then
        if sed -i "s/^${key}[[:space:]]*=.*/${key} = ${want}/" "$cfg" 2>/dev/null; then
            echo "[init] Set streamrip ${key}=${want} (was ${cur})."
        else
            echo "[warn] couldn't set streamrip ${key}=${want}. Mount /config read-write." >&2
        fi
    elif grep -q "^\[${section}\]" "$cfg" 2>/dev/null; then
        # Key absent (an older streamrip's config predates it), add it under its
        # section rather than skipping, which would silently keep streamrip's
        # default. Appending at EOF would land it under the wrong [table].
        if sed -i "/^\[${section}\]/a ${key} = ${want}" "$cfg" 2>/dev/null; then
            echo "[init] Added streamrip ${key}=${want} under [${section}]."
        else
            echo "[warn] couldn't add streamrip ${key}=${want}. Mount /config read-write." >&2
        fi
    else
        echo "[warn] streamrip config has no [${section}], can't enforce ${key}=${want}." >&2
    fi
}
enforce_streamrip downloads_enabled false database
enforce_streamrip add_singles_to_folder true filepaths
enforce_streamrip download_booklets false qobuz
enforce_streamrip check_for_updates false misc

# The streamrip config holds the Qobuz token once creds are set. The web/env
# write path lands 0600 (atomic mkstemp+replace); the seeded default arrives
# 0644 via cp, so bring it in line here.
chmod 600 "$STREAMRIP_DIR/config.toml" 2>/dev/null || true

export STREAMRIP_CONFIG="$STREAMRIP_DIR/config.toml"
# So `docker exec qobuz-librarian beet …` finds the real config + DB
# instead of falling back to ~/.config/beets/ (which is empty inside the
# container). The app reads BEETS_CONFIG_DIR/BEETS_DB_PATH itself; this
# is purely for ad-hoc CLI use.
export BEETSDIR="$BEETS_DIR"

# ── Privilege drop ────────────────────────────────────────────────────────────
# Run as a non-root uid:gid so downloaded/imported files aren't root-owned.
# Defaults to 1000:1000; set PUID/PGID to match the owner of your media share
# (find them with `id -u` / `id -g`). PUID=0 PGID=0 deliberately runs as root.
# gosu takes a numeric uid:gid directly; no account needs to exist.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
APP_USER="root"
case "${PUID}${PGID}" in
    *[!0-9]*)
        # gosu needs a numeric uid:gid; a name like "appuser" would make the
        # final exec fail fatally. Fail closed rather than silently running as
        # root: root must be the explicit, valid pair 0:0, never the accidental
        # result of a typo'd PUID/PGID.
        echo "[fatal] PUID/PGID must be numeric (got ${PUID}:${PGID}). Refusing to start; set a numeric PUID/PGID, or PUID=0 PGID=0 to run as root deliberately." >&2
        exit 1
        ;;
    *)
        # Canonicalize to plain decimal first: the root checks below compare
        # strings, and PUID=00 is still uid 0. With extra digits it would
        # slip past "= 0" and run as root while reading as non-root. (Not
        # $((...)): a leading zero makes sh parse 08/09 as bad octal.)
        PUID="$(printf '%s' "$PUID" | sed 's/^0*\([0-9]\)/\1/')"
        PGID="$(printf '%s' "$PGID" | sed 's/^0*\([0-9]\)/\1/')"
        # Root must be the explicit, deliberate pair 0:0. A mixed pair like
        # 0:1000 still runs UID 0 (full root), and 1000:0 grants the root
        # group. Both look like a normal PUID/PGID setup while quietly
        # keeping root privileges, so refuse them outright.
        if { [ "$PUID" = "0" ] && [ "$PGID" != "0" ]; } || \
           { [ "$PGID" = "0" ] && [ "$PUID" != "0" ]; }; then
            echo "[fatal] PUID=${PUID} PGID=${PGID} mixes root with a non-root id. Use a fully non-root pair, or PUID=0 PGID=0 to run as root deliberately." >&2
            exit 1
        fi
        APP_USER="${PUID}:${PGID}"
        # /music is the user's library, possibly a huge NAS mount, so it is
        # never recursively chowned; its permissions are theirs to manage (the
        # writability check below guides them if PUID doesn't match). The rest
        # are small app-managed volumes: chown them so a fresh deploy doesn't
        # warn "not writable" on every boot until the user fixes it by hand.
        # Only entries with the wrong owner are touched. chown(2) bumps ctime
        # even when nothing changes, which would break every sealed backup
        # receipt on every restart. Non-fatal on failure (a NAS-bound staging
        # just falls through to the writability warning).
        find "$BEETS_DIR" "$STREAMRIP_DIR" /data /staging \
            /upgrade_backups /collection_backups \
            \( ! -user "$PUID" -o ! -group "$PGID" \) \
            -exec chown -h "$APP_USER" -- {} + 2>/dev/null || true
        echo "[init] Running as ${APP_USER}."
        ;;
esac

# ── Writability diagnostics ───────────────────────────────────────────────────
# A NAS export that doesn't grant the run user write access is the most
# common failure; surface it clearly instead of failing cryptically later.
for d in /music /staging /data /upgrade_backups /collection_backups "$CONFIG_DIR"; do
    if [ "$APP_USER" = "root" ]; then
        [ -w "$d" ] && ok=yes || ok=no
    elif gosu "$APP_USER" test -w "$d" 2>/dev/null; then
        ok=yes
    else
        ok=no
    fi
    if [ "$ok" != "yes" ]; then
        echo "[warn] $d is not writable by ${APP_USER}." >&2
        echo "       On a NAS: set PUID/PGID to the share owner and grant" >&2
        echo "       that user write access, or downloads/imports will fail." >&2
    fi
done

run() {
    if [ "$APP_USER" = "root" ]; then
        exec "$@"
    else
        # gosu resets HOME to the user's passwd entry (or / for unknown UIDs).
        # Pass HOME explicitly so rip/streamrip don't try to write to /.config
        # on a read-only rootfs. APP_HOME is overridable for users running
        # --read-only with a custom tmpfs target (e.g. /var/tmp instead of
        # /tmp); /tmp is the default because every plain docker run has one.
        exec gosu "$APP_USER" env HOME="${APP_HOME:-/tmp}" "$@"
    fi
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "${1:-web}" in
    web)
        run uvicorn qobuz_librarian.web.app:app \
            --host "${WEB_HOST:-0.0.0.0}" \
            --port "${WEB_PORT:-8666}" \
            --workers 1 \
            --proxy-headers \
            --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
            --no-server-header
        ;;
    cli)
        shift
        run qobuz-librarian "$@"
        ;;
    -*)
        run qobuz-librarian "$@"
        ;;
    *)
        run "$@"
        ;;
esac
