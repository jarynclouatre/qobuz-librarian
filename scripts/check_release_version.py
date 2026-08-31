#!/usr/bin/env python3
"""Refuse a release tag that disagrees with the packaged application version,
or whose changelog entry is still marked Unreleased."""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_version(path: Path = ROOT / "pyproject.toml") -> str:
    with path.open("rb") as handle:
        value = tomllib.load(handle).get("project", {}).get("version")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: project.version is missing")
    return value.strip()


def check_release_tag(tag: str, version: str) -> str | None:
    expected = f"v{version}"
    if tag == expected:
        return None
    return f"release tag {tag!r} does not match packaged version {version!r} ({expected!r})"


def check_changelog_dated(version: str, path: Path = ROOT / "CHANGELOG.md") -> str | None:
    heading = f"## [{version}] - "
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(heading):
            when = line.removeprefix(heading).strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when):
                return None
            return f"CHANGELOG.md: [{version}] reads {when!r}; date it before releasing"
    return f"CHANGELOG.md has no [{version}] entry"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_release_version.py RELEASE_TAG", file=sys.stderr)
        return 2
    try:
        version = project_version()
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    problem = check_release_tag(argv[1], version) or check_changelog_dated(version)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    print(f"Release tag {argv[1]} matches packaged version {version}, "
          "and its changelog entry is dated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
