"""The atomic writer must never leave a store torn, empty, or half of two
writers' output - the failure modes the corrupt-file loader exists to survive
must not be ones the app itself manufactures."""
import json
import os
import threading

import pytest

from qobuz_librarian import state_file


def test_failed_replace_leaves_the_original_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "store.json"
    state_file.write_json(path, {"kept": 1})

    real_replace = os.replace

    def broken_replace(src, dst):
        if str(dst) == str(path):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", broken_replace)
    try:
        state_file.write_json(path, {"lost": 2})
    except OSError:
        pass
    else:
        raise AssertionError("write_json must surface the failure")
    monkeypatch.undo()

    assert json.loads(path.read_text()) == {"kept": 1}
    leftovers = [p for p in tmp_path.iterdir() if p.name != "store.json"]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_concurrent_writers_never_tear_the_store(tmp_path):
    # The old fixed-".tmp" writers let two processes interleave on one temp
    # name and rename a half-written file into place. Unique temps make every
    # landed generation whole; hammer it and check each observed state parses.
    path = tmp_path / "store.json"
    errors = []

    def writer(n):
        try:
            for i in range(40):
                state_file.write_json(
                    path, {"writer": n, "i": i, "pad": "x" * 4096})
        except Exception as e:  # noqa: BLE001 - the test reports any failure
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    seen = 0
    while any(t.is_alive() for t in threads):
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        assert set(data) == {"writer", "i", "pad"}
        seen += 1
    for t in threads:
        t.join()
    assert not errors
    assert seen > 0
    final = json.loads(path.read_text())
    assert final["i"] == 39




def test_store_lock_refuses_symlink_without_touching_target(tmp_path):
    path = tmp_path / "store.json"
    victim = tmp_path / "unrelated.json"
    original = '{"must_survive": true}'
    victim.write_text(original, encoding="utf-8")
    path.with_name(path.name + ".lock").symlink_to(victim)

    with pytest.raises(OSError):
        with state_file.store_lock(path):
            pass

    assert victim.read_text(encoding="utf-8") == original
