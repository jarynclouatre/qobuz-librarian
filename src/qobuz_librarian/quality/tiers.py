"""streamrip quality-tier constants and cap detection."""
from qobuz_librarian import config as cfg
from qobuz_librarian.integrations import downsample_engine

# Highest (bit_depth, sample_rate_hz) streamrip delivers per quality tier:
# 2=16/44.1, 3=24-bit up to 96kHz, 4=24-bit up to 192kHz. 320kbps MP3 is not
# offered: config coerces a stale STREAMRIP_QUALITY=0/1 to 2 at load and the
# Settings page only accepts 2-4.
_STREAMRIP_QUALITY_CAPS = {
    2: (16, 44100),
    3: (24, 96000),
    4: (24, 192000),
}


def streamrip_quality_cap(tier=None):
    """Return (max_bit_depth, max_sample_rate_hz) streamrip will actually
    deliver at the current quality setting.

    rip.py invokes streamrip with `-q cfg.STREAMRIP_QUALITY`, and that flag
    overrides streamrip's own config - so STREAMRIP_QUALITY (env / Settings)
    is authoritative. config validates it to a 2-4 tier at load and the
    Settings page only accepts those values, so this is a live tier lookup:
    reading it fresh each call means a quality change takes effect at once. A
    value that somehow slips past coercion degrades to the top tier rather than
    raising mid-scan.
    """
    tier = cfg.STREAMRIP_QUALITY if tier is None else tier
    return _STREAMRIP_QUALITY_CAPS.get(tier, _STREAMRIP_QUALITY_CAPS[4])


def downsample_target_rate(sr_hz):
    """Sample rate (Hz) a file ends up at after the downsample hook runs.

    Delegates to the engine's target_rate() so the integer-ratio family table
    lives in exactly one place - a second copy here could drift and make scan
    estimates disagree with the actual downsampler. Unlike the engine's version,
    this returns the input for the no-change case rather than None, so callers
    can chain it without a guard.
    """
    return downsample_engine.target_rate(sr_hz) or sr_hz


def format_quality(bits, rate):
    if not bits or not rate:
        return "?"
    return f"{bits}/{rate / 1000:g}"
