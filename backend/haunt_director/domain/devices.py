"""Devices bind a friendly name to a profile + DMX address (see docs/how-it-works.md)."""
from __future__ import annotations

from enum import Enum
from pydantic import BaseModel


class DeviceKind(str, Enum):
    DMX_FIXTURE = "dmx_fixture"
    DMX_FOG = "dmx_fog"
    AUDIO_SINK = "audio_sink"
    PRESENCE_SENSOR = "presence_sensor"
    PROP = "prop"
    STATIC_RGB = "static_rgb"         # standalone solar/battery RGB spotlight (not Pi-controllable)


class Device(BaseModel):
    id: str
    name: str
    kind: DeviceKind
    profile_id: str | None = None
    dmx_address: int | None = None    # 1-based start channel
    enabled: bool = True
    # simulator placement (normalized 0..1 over the house photo)
    sim_position: dict | None = None  # {"x":.6, "y":.57}
    aim: dict | None = None           # {"x":.58, "y":.25} — where the beam lands
    spread: float = 1.0               # beam width multiplier
    # follow another device of the same kind: in any scene where this one has no
    # events of its own, it plays that device's part (domain/rig.py)
    follows: str | None = None
    # static RGB fixtures: a global, always-on colour we can't drive over DMX —
    # we just record what the physical light is set to so the yard preview matches.
    static_color: str = "#ff3030"     # one of the 8 preset colours (hex)
    static_mode: str = "fixed"        # "fixed" | "auto" (auto cycles through colours)
    static_brightness: float = 1.0    # 0..1
    static_on: bool = True            # is the light currently on in the yard
