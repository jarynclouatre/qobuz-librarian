#!/usr/bin/env python3
"""Check that a built wheel carries the compiled web stylesheet."""

import sys
import zipfile
from pathlib import Path

CSS_PATH = "qobuz_librarian/web/static/dist/app.css"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_wheel_assets.py PATH_TO_WHEEL", file=sys.stderr)
        return 2

    wheel = Path(sys.argv[1])
    try:
        with zipfile.ZipFile(wheel) as archive:
            css = archive.read(CSS_PATH)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        print(f"{wheel}: compiled stylesheet is missing or unreadable: {exc}",
              file=sys.stderr)
        return 1

    if len(css) < 1000 or b"--tw-" not in css:
        print(f"{wheel}: packaged stylesheet does not look compiled",
              file=sys.stderr)
        return 1

    print(f"{wheel}: packaged {CSS_PATH} ({len(css)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
