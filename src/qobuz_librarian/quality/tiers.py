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

    rip.py invokes streamrip with `-q cfg.STREAMRIP_QUALITY`, which overrides
    streamrip's own config. Read fresh each call, so a quality change takes
    effect at once.
    """
    tier = cfg.STREAMRIP_QUALITY if tier is None else tier
    return _STREAMRIP_QUALITY_CAPS.get(tier, _STREAMRIP_QUALITY_CAPS[4])


def tier_for(bits, rate):
    """The lowest tier that delivers ``bits``/``rate`` (Hz), the top one for
    anything above it, or None when either is unknown."""
    if not bits or not rate:
        return None
    return next((tier for tier, (cap_bits, cap_rate)
                 in sorted(_STREAMRIP_QUALITY_CAPS.items())
                 if bits <= cap_bits and rate <= cap_rate), 4)


def downsample_target_rate(sr_hz):
    """Sample rate (Hz) a file ends up at after the downsample hook runs.

    The engine's target_rate(), except that the no-change case returns the
    input rather than None.
    """
    return downsample_engine.target_rate(sr_hz) or sr_hz


def delivered_rate(sr_hz, tier=None):
    """Sample rate (Hz) the tier delivers from a ``sr_hz`` master: the
    highest rate of the master's own 44.1 or 48 kHz family within the tier
    cap, so 176.4 kHz comes at 88.2 kHz under a 96 kHz cap."""
    cap = streamrip_quality_cap(tier)[1]
    if sr_hz <= cap:
        return sr_hz
    rate = downsample_target_rate(sr_hz)
    if rate > cap:
        return cap
    while rate * 2 <= cap:
        rate *= 2
    return rate


def format_quality(bits, rate):
    if not bits or not rate:
        return "?"
    return f"{bits}/{rate / 1000:g}"
