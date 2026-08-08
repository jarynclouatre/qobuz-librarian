"""Find library FLACs stored above CD rate and worth shrinking.

Local housekeeping: walk the library, read each FLAC's sample rate, and group
the high-rate files into per-album candidates the downsample mode offers for
review. No Qobuz lookup - unlike the upgrade scan the answer comes entirely off
disk, so it runs without credentials.
"""
from dataclasses import dataclass
from pathlib import Path

from qobuz_librarian.integrations.downsample_engine import (
    HAVE_DOWNSAMPLE,
    scan_dir_for_hires,
)
from qobuz_librarian.library.scanner import list_artist_album_dirs
from qobuz_librarian.ui_cli.colors import format_size


def _khz(hz):
    return f"{hz / 1000:.1f}kHz".replace(".0kHz", "kHz")


@dataclass
class DownsampleCandidate:
    album_dir: Path
    artist: str
    title: str
    n_hires: int          # high-rate tracks (the ones that get shrunk)
    n_flac: int           # all FLACs in the folder
    source_rates: list    # sorted unique source sample rates (Hz)
    target_rates: list    # sorted unique target sample rates (Hz)
    est_saving: int       # estimated bytes reclaimable (audio scaled by the rate cut)

    @property
    def rate_label(self):
        src = "/".join(_khz(r) for r in self.source_rates)
        dst = _khz(self.target_rates[0]) if len(self.target_rates) == 1 else "CD rate"
        return f"{src} → {dst}"

    @property
    def detail(self):
        part = "" if self.n_hires == self.n_flac else f" · {self.n_hires}/{self.n_flac} tracks"
        return f"{self.rate_label}{part} · ~{format_size(self.est_saving)} reclaimable"


def scan_artist_for_downsample(artist_dir: Path):
    """High-rate albums under one artist folder, as review candidates.

    Mirrors quality.decision.scan_artist_for_upgrades' per-artist shape so the
    CLI walk and the web fan-out drive it the same way. Returns [] when ffmpeg
    isn't available to resample.
    """
    if not HAVE_DOWNSAMPLE:
        return []
    artist = artist_dir.name
    out = []
    for album_dir in list_artist_album_dirs(artist_dir):
        info = scan_dir_for_hires(album_dir)
        hires = info["hires"]
        if not hires:
            continue
        # Scale each file's audio portion by its rate cut (96→48 ≈ half the
        # samples, so ≈ half the audio bytes).
        est = sum(int(h["audio_size"] * (1 - h["target"] / h["sr"])) for h in hires)
        out.append(DownsampleCandidate(
            album_dir=album_dir,
            artist=artist,
            title=album_dir.name,
            n_hires=len(hires),
            n_flac=info["n_flac"],
            source_rates=sorted({h["sr"] for h in hires}),
            target_rates=sorted({h["target"] for h in hires}),
            est_saving=est,
        ))
    return out
