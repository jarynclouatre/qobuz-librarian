from qobuz_librarian import config as cfg
from qobuz_librarian.api import auth
from qobuz_librarian.library import scanner
from qobuz_librarian.modes import album as album_mode
from qobuz_librarian.web import flows


def test_queued_mix_rechecks_files_and_stops_after_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MUSIC_ROOT", tmp_path)
    folder = tmp_path / "The Beatles" / "Rubber Soul (1965)"
    folder.mkdir(parents=True)
    original = folder / "Drive My Car.flac"
    original.write_bytes(b"original")
    scanner.clear_scan_caches()
    remix = {
        "id": "rubber-soul-2026", "title": "Rubber Soul", "version": "2026 Mix",
        "artist": {"name": "The Beatles"}, "maximum_bit_depth": 16,
        "tracks": {"items": [{"id": "drive-2026", "title": "Drive My Car", "version": "2026 Mix"}]},
    }
    args = flows.build_args()
    args.yes = False
    args.query = []
    choices = []

    def choose(*_args):
        choices.append(remix)
        if len(choices) == 2:
            original.write_bytes(b"changed outside the app")
            raise auth.Aborted("user cancelled at album query")
        return remix

    downloaded = []

    def download(album, *_args, **kwargs):
        downloaded.append((album, kwargs))
        return {"result": "failed", "attention": True}

    monkeypatch.setattr(album_mode, "resolve_album_from_args", choose)
    monkeypatch.setattr(album_mode, "ask", lambda _prompt: "q")
    monkeypatch.setattr(album_mode, "_download_album_now", download)
    assert album_mode.run_album_mode(args, "tok", loop=True) != 0
    assert not downloaded

    monkeypatch.setattr(album_mode, "resolve_album_from_args", lambda *_a: remix)
    answers = iter(["q", "f"])
    monkeypatch.setattr(album_mode, "ask", lambda _prompt: next(answers))
    assert album_mode.run_album_mode(args, "tok", loop=True) != 0
    assert len(downloaded) == 1
    assert downloaded[0][1]["keep_edition"]
    assert original.read_bytes() == b"changed outside the app"
