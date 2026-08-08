import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_release_version.py"


def _run(tag: str):
    return subprocess.run(
        [sys.executable, str(CHECK), tag],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_tag_must_match_the_packaged_version():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]

    accepted = _run(f"v{version}")
    assert accepted.returncode == 0
    assert f"version {version}" in accepted.stdout

    refused = _run("v999.0.0")
    assert refused.returncode == 1
    assert "does not match packaged version" in refused.stderr
