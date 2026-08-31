import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_release_version.py"


def _tree(tmp_path: Path, heading: str) -> Path:
    """A minimal checkout the script can resolve itself against."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(CHECK, tmp_path / "scripts" / "check_release_version.py")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "3.2.1"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n{heading}\n\n- Things.\n")
    return tmp_path


def _run(tree: Path, tag: str):
    return subprocess.run(
        [sys.executable, str(tree / "scripts" / "check_release_version.py"), tag],
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_tag_must_match_the_packaged_version(tmp_path):
    tree = _tree(tmp_path, "## [3.2.1] - 2026-08-22")

    accepted = _run(tree, "v3.2.1")
    assert accepted.returncode == 0

    refused = _run(tree, "v999.0.0")
    assert refused.returncode == 1
    assert "does not match packaged version" in refused.stderr


def test_release_refused_while_the_changelog_entry_is_undated(tmp_path):
    tree = _tree(tmp_path, "## [3.2.1] - Unreleased")

    refused = _run(tree, "v3.2.1")
    assert refused.returncode == 1
    assert "date it before releasing" in refused.stderr
