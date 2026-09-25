"""Tests for qobuz_librarian.library.tags - the gnarly bits."""
from qobuz_librarian.library.tags import (
    beets_sanitize,
    strip_album_decorations,
)


def test_beets_sanitize_matches_beets_on_disk_names():
    assert beets_sanitize("AC/DC") == "AC_DC"
    assert beets_sanitize("hello:world") == "hello_world"
    # Leading/trailing dots turn into _ (not dropped) exactly as beets writes
    # them, so a folder like "...And Justice for All" resolves on a scan
    # instead of being reported missing and re-downloaded.
    assert beets_sanitize("...And Justice for All") == "_..And Justice for All"
    assert beets_sanitize("Artist.") == "Artist_"
    # A `[$year] $album` path template still matches the album title.
    assert strip_album_decorations("[1971] Hunky Dory") == "Hunky Dory"
