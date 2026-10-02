"""Turn a desired fixture state into absolute DMX channel values.

The single place that knows how a colour/brightness/strobe maps onto channels.
Safety rule (docs/fixture-profiles.md): any channel with `required_band` is pinned
to that band's midpoint on EVERY resolve, so a fixture can never wander into its
built-in auto-program during a scare.
"""
from __future__ import annotations

from .profiles import ChannelRole, FixtureProfile

RGB = tuple[int, int, int]


# Optional global ceiling on strobe rate (Hz), set once at startup from
# HD_STROBE_MAX_HZ. None = no cap. Everything that drives a fixture resolves
# here (scenes AND Live cards), so this one cap covers the whole rig.
STROBE_MAX_HZ: float | None = None


def set_strobe_cap(hz) -> None:
    global STROBE_MAX_HZ
    try:
        v = float(hz or 0)
    except (TypeError, ValueError):
        v = 0.0
    STROBE_MAX_HZ = v if v > 0 else None


def capped_strobe_hz(hz):
    """The rate a fixture will actually strobe at (for the simulator too)."""
    if not hz:
        return hz
    return min(float(hz), STROBE_MAX_HZ) if STROBE_MAX_HZ else hz


def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def resolve_fixture(profile: FixtureProfile, dmx_address: int, *,
                    rgb: RGB | None = None, brightness: int = 255,
                    strobe_hz: float | None = None) -> dict[int, int]:
    """Return {absolute_channel(1-based): value} for the given state."""
    out: dict[int, int] = {}

    def put(offset: int, value: int) -> None:
        out[dmx_address + offset] = _clamp(value)

    # 1) every channel starts at its declared default
    for ch in profile.channels:
        put(ch.offset, ch.default)

    # 2) colour + brightness
    has_dimmer = profile.has_master_dimmer and profile.channel(ChannelRole.DIMMER)
    if rgb is not None:
        r, g, b = rgb
        if has_dimmer:
            scale = 1.0                       # dimmer carries intensity
        else:
            scale = brightness / 255.0        # bake intensity into colour
        for role, val in ((ChannelRole.RED, r), (ChannelRole.GREEN, g), (ChannelRole.BLUE, b)):
            off = profile.offset_for(role)
            if off is not None:
                put(off, val * scale)
        w_off = profile.offset_for(ChannelRole.WHITE)
        if w_off is not None:
            put(w_off, 0)                     # explicit: no white unless asked
    if has_dimmer:
        put(profile.offset_for(ChannelRole.DIMMER), brightness)

    # 3) strobe — held value on the strobe channel, never frame-by-frame (§4.3)
    strobe = profile.channel(ChannelRole.STROBE)
    if strobe is not None:
        if not strobe_hz:
            put(strobe.offset, strobe.off_band.mid if strobe.off_band else 0)
        else:
            put(strobe.offset, _strobe_value(strobe, capped_strobe_hz(strobe_hz)))

    # 4) SAFETY: pin every required_band channel to its band midpoint, always
    for ch in profile.channels:
        if ch.required_band:
            put(ch.offset, profile.band_mid(ch.required_band, ch.offset))

    return out


def _strobe_value(strobe, hz: float) -> int:
    """Map a strobe rate (Hz) into the fixture's strobe channel value."""
    rb = strobe.rate_band
    if not rb:
        return 255  # no band info: full-rate strobe
    pts = rb.get("points")
    if pts:
        # Fixture-calibrated curve: [[hz, dmx_value], ...] measured on the real
        # fixture (its strobe rate is very non-linear). Interpolate the DMX value
        # for the requested rate so the light strobes at ~hz, matching the sim.
        p = sorted(pts)
        if hz <= p[0][0]:
            return _clamp(p[0][1])
        if hz >= p[-1][0]:
            return _clamp(p[-1][1])
        for i in range(1, len(p)):
            h0, v0 = p[i - 1]
            h1, v1 = p[i]
            if hz <= h1:
                f = (hz - h0) / (h1 - h0) if h1 > h0 else 0.0
                return _clamp(v0 + f * (v1 - v0))
        return _clamp(p[-1][1])
    lo, hi = rb["lo"], rb["hi"]
    min_hz, max_hz = rb.get("min_hz", 1.0), rb.get("max_hz", 20.0)
    frac = (hz - min_hz) / (max_hz - min_hz) if max_hz > min_hz else 1.0
    frac = max(0.0, min(1.0, frac))
    return _clamp(lo + frac * (hi - lo))
