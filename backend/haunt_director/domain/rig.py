"""The rig as a whole: where each device sits in DMX, how big the channel block
must be, which lights follow which, and what's wrong with it. Pure (no I/O), so
the API, the System tab and the tests all share one set of rules.

Followers. A light can follow another light: in any scene where it has no
events of its own, it plays the other light's part. That's how a light added
after the scenes were written still joins every one of them. Foggers can follow
foggers the same way.

The channel block. The output frame covers DMX 1 to the block's end. The block
is at least the configured size (HD_DMX_CHANNEL_COUNT, 64 by default) and grows
on its own to reach the last channel any device uses, up to one universe (512).
"""
from __future__ import annotations

import re

from .devices import Device, DeviceKind
from .profiles import ChannelRole, FixtureProfile

MAX_CHANNEL = 512                   # one DMX universe
SLOT = 16                           # suggested addresses start on 1, 17, 33... like the reference rig
DMX_KINDS = (DeviceKind.DMX_FIXTURE, DeviceKind.DMX_FOG)
LIGHT_ROLES = (ChannelRole.RED, ChannelRole.GREEN, ChannelRole.BLUE, ChannelRole.WHITE, ChannelRole.DIMMER)


def _kind(d: Device) -> str:
    return getattr(d.kind, "value", d.kind)


def is_dmx(d: Device) -> bool:
    return _kind(d) in ("dmx_fixture", "dmx_fog")


def profile_kind(p: FixtureProfile) -> str:
    """'fog' for a fogger profile, 'light' for anything with a color or dimmer channel."""
    if p.offset_for(ChannelRole.FOG) is not None:
        return "fog"
    if any(p.offset_for(r) is not None for r in LIGHT_ROLES):
        return "light"
    return "other"


def span(d: Device, profiles: dict[str, FixtureProfile]) -> tuple[int, int] | None:
    """(first, last) DMX channel a device uses, or None if it has no address or
    no known profile."""
    if not is_dmx(d) or d.dmx_address is None:
        return None
    p = profiles.get(d.profile_id or "")
    if p is None:
        return None
    n = max(1, int(p.channel_count or len(p.channels) or 1))
    return int(d.dmx_address), int(d.dmx_address) + n - 1


def needed_end(devices, profiles) -> int:
    """The last DMX channel any device uses (0 for an empty rig)."""
    ends = [s[1] for d in devices if (s := span(d, profiles))]
    return max(ends, default=0)


def block_end(devices, profiles, configured_end: int) -> int:
    """The output frame's last channel: the configured block, grown to cover
    every device, never past one universe."""
    return max(1, min(MAX_CHANNEL, max(int(configured_end), needed_end(devices, profiles))))


# ---------------------------------------------------------------- followers
def follow_map(devices) -> dict[str, str]:
    """{device: the device it follows}, for enabled devices whose 'follows'
    names an existing device of the same kind (anything else is ignored, and
    reported by issues())."""
    by_id = {d.id: d for d in devices}
    out = {}
    for d in devices:
        lead = by_id.get(d.follows or "")
        if lead is None or lead.id == d.id or not d.enabled or _kind(lead) != _kind(d) or not is_dmx(d):
            continue
        out[d.id] = lead.id
    return out


def part_source(dev_id: str, tracks: dict, follows: dict[str, str]) -> str | None:
    """Whose events a device plays in a scene with these tracks: its own if it
    has any, else the first device up its follow chain that has some. None if
    it plays nothing. Chains are followed; loops are cut."""
    if tracks.get(dev_id):
        return dev_id
    seen = {dev_id}
    cur = follows.get(dev_id)
    while cur and cur not in seen:
        if tracks.get(cur):
            return cur
        seen.add(cur)
        cur = follows.get(cur)
    return None


def _creates_loop(dev_id: str, lead_id: str, devices) -> bool:
    follows = {d.id: d.follows for d in devices if d.follows}
    follows[dev_id] = lead_id
    seen, cur = {dev_id}, lead_id
    while cur:
        if cur in seen:
            return True
        seen.add(cur)
        cur = follows.get(cur)
    return False


# ---------------------------------------------------------------- checks
def issues(devices, profiles) -> list[dict]:
    """Everything wrong with a rig, worst first: [{level, device, devices,
    message}]. 'device' is the one to fix; 'devices' is every device involved
    (both lights in an overlap). 'error' means that device can't work as set
    up; 'warn' means it works but probably not the way you meant."""
    devices = list(devices)
    out: list[dict] = []

    def add(level, dev, msg, also=()):
        ids = [dev.id] if dev else []
        out.append({"level": level, "device": dev.id if dev else None,
                    "devices": ids + [o.id for o in also if o.id not in ids], "message": msg})

    spans = []
    for d in devices:
        if not is_dmx(d):
            continue
        what = "fogger" if _kind(d) == "dmx_fog" else "light"
        if not d.profile_id:
            add("error", d, f"{d.name} has no fixture profile.")
            continue
        p = profiles.get(d.profile_id)
        if p is None:
            add("error", d, f"{d.name} uses profile '{d.profile_id}', which isn't installed.")
            continue
        pk = profile_kind(p)
        if what == "fogger" and pk != "fog":
            add("error", d, f"{d.name} is a fogger, but its profile has no fog channel.")
        elif what == "light" and pk != "light":
            add("error", d, f"{d.name} is a light, but its profile ({p.vendor} {p.model}) isn't a light profile.")
        if d.dmx_address is None:
            add("error", d, f"{d.name} has no DMX address.")
            continue
        s = span(d, profiles)
        if s[0] < 1 or s[1] > MAX_CHANNEL:
            add("error", d, f"{d.name} would use channels {s[0]} to {s[1]}, outside one DMX universe (1 to {MAX_CHANNEL}).")
            continue
        spans.append((d, s))
    for i, (a, sa) in enumerate(spans):
        for b, sb in spans[i + 1:]:
            if sa[0] <= sb[1] and sb[0] <= sa[1]:
                add("error", b, f"{b.name} (channels {sb[0]} to {sb[1]}) overlaps {a.name} "
                                f"(channels {sa[0]} to {sa[1]}). Give one of them a different address.", also=(a,))
    by_id = {d.id: d for d in devices}
    for d in devices:
        if not d.follows:
            continue
        lead = by_id.get(d.follows)
        if lead is None:
            add("warn", d, f"{d.name} follows a device that no longer exists, so it only plays its own parts.")
        elif lead.id == d.id:
            add("warn", d, f"{d.name} is set to follow itself, which does nothing.")
        elif _kind(lead) != _kind(d):
            add("warn", d, f"{d.name} follows {lead.name}, but lights can only follow lights and foggers only foggers.")
        elif _creates_loop(d.id, lead.id, devices):
            add("warn", d, f"{d.name} and {lead.name} follow each other in a loop; neither borrows a part.")
    out.sort(key=lambda x: 0 if x["level"] == "error" else 1)
    return out


# ---------------------------------------------------------------- helpers for adding devices
def suggest_address(devices, profiles, width: int, exclude: str | None = None) -> int | None:
    """The first free address for a device `width` channels wide. Prefers the
    next 16-channel slot (1, 17, 33, ...), so there's room to change modes
    later without re-addressing everything; falls back to any gap that fits."""
    width = max(1, int(width))
    taken = [s for d in devices if d.id != exclude and (s := span(d, profiles))]

    def free(a):
        e = a + width - 1
        return e <= MAX_CHANNEL and all(not (a <= t[1] and t[0] <= e) for t in taken)

    for a in range(1, MAX_CHANNEL + 1, SLOT):
        if free(a):
            return a
    for a in range(1, MAX_CHANNEL - width + 2):
        if free(a):
            return a
    return None


def new_id(name: str, existing) -> str:
    """A stable id from a name: 'Porch Light 2' -> 'porch_light_2' (unique)."""
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:32] or "device"
    taken = set(existing)
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def default_placement(devices, kind: str) -> dict:
    """Somewhere sensible on the stage picture for a new device: along the
    ground, in the widest gap between the devices already there, beam up."""
    xs = sorted(float(d.sim_position.get("x", 0.5)) for d in devices
                if d.sim_position and isinstance(d.sim_position, dict))
    edges = [0.06] + [min(0.94, max(0.06, x)) for x in xs] + [0.94]
    gaps = [(b - a, (a + b) / 2) for a, b in zip(edges, edges[1:])]
    x = round(max(gaps)[1], 4) if gaps else 0.5
    if kind == "dmx_fog":
        return {"sim_position": {"x": x, "y": 0.6}}
    return {"sim_position": {"x": x, "y": 0.64}, "aim": {"x": x, "y": 0.3}}
