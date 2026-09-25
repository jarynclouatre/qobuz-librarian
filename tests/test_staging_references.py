

import pytest

from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import staging as staging_module
from qobuz_librarian.integrations.staging import park_trees


@pytest.fixture
def staging(tmp_path, monkeypatch):
    root = tmp_path / "staging"
    monkeypatch.setattr(cfg, "STAGING_DIR", root)
    return root


def test_a_changed_group_can_still_be_cleared_on_the_users_say_so(staging):
    source = staging / "Artist - Album"
    source.mkdir(parents=True)
    (source / "01.flac").write_bytes(b"original")
    group = park_trees([source], "failed import")
    assert group is not None

    (group.trees[0].path / "01.flac").write_bytes(b"not what was parked")
    assert staging_module.discard_group(group) is False
    assert group.path.exists()

    elsewhere = staging / "elsewhere"
    elsewhere.mkdir()
    assert staging_module.discard_group_unchecked(elsewhere) is False
    assert elsewhere.exists()

    assert staging_module.discard_group_unchecked(group.path) is True
    assert not group.path.exists()
