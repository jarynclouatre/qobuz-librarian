#!/usr/bin/env bash
# Regenerate docker/image-lock.txt, the pinned dependency set the runtime image
# installs. Run it after bumping the streamrip commit or beets version in the
# Dockerfile, or when you deliberately want to pull in newer library releases.
#
#   ./scripts/lock-image-deps.sh
#
# It resolves the whole set fresh inside the same base image the Dockerfile
# builds on, then freezes the result. The streamrip ref and beets pin are read
# straight from the Dockerfile so they can't drift; the app's own dependencies
# come from requirements.txt so that stays the single source for those. After
# running, review the diff, rebuild, and run scripts/smoke_test.sh before you
# commit. A fresh resolve can pull newer versions that need a quick sanity pass.
set -euo pipefail

cd "$(dirname "$0")/.."

streamrip_ref=$(grep -oE 'streamrip @ git\+https://[^"]+' Dockerfile | head -1)
beets_pin=$(grep -oE 'beets==[0-9.]+' Dockerfile | head -1)
# Resolve inside the same base image the runtime uses. Read it from the
# Dockerfile like the pins above. A hard-coded python tag drifts: a lock
# resolved on 3.12 can pick version-conditional deps and wheels the shipped
# 3.14 interpreter never saw.
base_image=$(grep -oE '^FROM python:[^ ]+' Dockerfile | head -1 | cut -d' ' -f2)
if [ -z "$streamrip_ref" ] || [ -z "$beets_pin" ] || [ -z "$base_image" ]; then
    echo "couldn't read the streamrip/beets/python pins from Dockerfile" >&2
    exit 1
fi

echo "==> Resolving image deps (${base_image}, streamrip ${streamrip_ref##*@}, ${beets_pin})"
# streamrip and beets are installed --no-deps in the image because they cap a
# few helpers (Pillow, aiofiles, tomlkit) far below what the librarian runs and
# verifies. Here they install with deps so the resolver picks a complete,
# self-consistent set; the freeze is what the image then pins to.
frozen=$(docker run --rm -i -v "$PWD/requirements.txt:/tmp/requirements.txt:ro" \
    "$base_image" bash -s <<EOF
set -e
apt-get update -qq && apt-get install -y -qq \
    git build-essential zlib1g-dev libjpeg62-turbo-dev >/dev/null 2>&1
pip install --no-cache-dir -q -r /tmp/requirements.txt >/dev/null
pip install --no-cache-dir -q "$streamrip_ref" "syncedlyrics>=1.0" >/dev/null
pip install --no-cache-dir -q --no-deps "$beets_pin" >/dev/null
pip install --no-cache-dir -q confuse jellyfish lap mediafile munkres packaging \
    "pillow>=12.2.0" platformdirs pyacoustid pyyaml requests requests-ratelimiter \
    typing_extensions unidecode >/dev/null
# streamrip caps a few helpers below the versions the app requires. Re-apply
# the app lock last so image-lock.txt matches the runtime contract.
pip install --no-cache-dir -q -r /tmp/requirements.txt >/dev/null
pip freeze
EOF
)

{
    echo "# Pinned dependency set for the runtime image. streamrip and beets pull a"
    echo "# large transitive tree; without this lock pip would resolve each one to"
    echo "# latest on every rebuild, so two builds of the same Dockerfile could ship"
    echo "# different library versions. These are a verified, self-consistent set."
    echo "# The Dockerfile installs streamrip and beets --no-deps (they cap Pillow,"
    echo "# aiofiles and tomlkit below what the librarian runs and verifies), then"
    echo "# installs this file. Don't edit by hand. Regenerate with"
    echo "# scripts/lock-image-deps.sh."
    # streamrip and beets are installed --no-deps in the image (the Dockerfile
    # pins them directly), so drop them here. Listing beets would drag in its
    # heavy numba/scipy extras the librarian never uses.
    echo "$frozen" | grep -vE '^-e |^# Editable|^streamrip @ |^beets==|^qobuz-librarian' | sort -f
} > docker/image-lock.txt

python3 scripts/check_image_lock.py
echo "==> Wrote docker/image-lock.txt ($(grep -cE '==' docker/image-lock.txt) packages pinned)"
