"""Fixture profiles — data, not code (see docs/fixture-profiles.md)."""
from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class ChannelRole(str, Enum):
    DIMMER = "dimmer"
    RED = "red"; GREEN = "green"; BLUE = "blue"; WHITE = "white"
    AMBER = "amber"; UV = "uv"
    STROBE = "strobe"
    FUNCTION = "function"   # banded: DMX-control vs auto modes
    MACRO = "macro"; SPEED = "speed"; MODE = "mode"
    FOG = "fog"; FAN = "fan"; UNUSED = "unused"


class Band(BaseModel):
    lo: int
    hi: int

    @property
    def mid(self) -> int:
        return (self.lo + self.hi) // 2


class ChannelDef(BaseModel):
    offset: int                       # 0-based within the fixture
    role: ChannelRole
    name: str = ""
    default: int = 0
    off_band: Band | None = None      # fog/strobe "off" range (not always 0)
    rate_band: dict | None = None     # strobe: {lo, hi, min_hz, max_hz}
    required_band: str | None = None  # pin this channel to a named band, always
    bands: list[dict] = Field(default_factory=list)


class FixtureProfile(BaseModel):
    schema_version: int = 1
    id: str
    vendor: str = ""
    model: str = ""
    mode_name: str = ""
    channel_count: int
    has_master_dimmer: bool = False
    channels: list[ChannelDef]

    def offset_for(self, role: ChannelRole) -> int | None:
        for ch in self.channels:
            if ch.role == role:
                return ch.offset
        return None

    def channel(self, role: ChannelRole) -> ChannelDef | None:
        for ch in self.channels:
            if ch.role == role:
                return ch
        return None

    def band_mid(self, name: str, offset: int) -> int:
        """Midpoint value for a named band on the channel at `offset`."""
        for ch in self.channels:
            if ch.offset == offset:
                for b in ch.bands:
                    if b.get("semantic") == name or b.get("label") == name:
                        return (b["lo"] + b["hi"]) // 2
        return 0
