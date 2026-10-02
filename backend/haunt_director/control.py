"""Manual control service: friendly device commands -> resolved DMX -> the
output bus's MANUAL layer.

Nothing here writes to FPP directly any more (see output.py). A Live-card
setting is an override that sits on top of whatever scene is playing, for that
fixture only, until it is released. A fog burst is an override that expires on
its own; the bus drops it the instant it lapses, so fog can't outlive its cap.
"""
from __future__ import annotations

import time

from .domain.devices import Device, DeviceKind
from .domain.profiles import ChannelRole
from .domain.resolve import resolve_fixture
from .fpp import FppClient
from .store import Store


class ControlService:
    def __init__(self, fpp: FppClient, store: Store, bus, fog_max_seconds: float = 8.0):
        self.fpp = fpp          # read-only use (health); all writes go through the bus
        self.store = store
        self.bus = bus
        self.fog_max_seconds = fog_max_seconds

    async def set_fixture(self, device_id: str, *, rgb=None, brightness: int = 255,
                          strobe_hz: float | None = None) -> dict[int, int]:
        """Override one fixture on top of any running scene until released."""
        dev = self._require(device_id)
        prof = self.store.profile_for(dev)
        if prof is None:
            raise ValueError(f"device {device_id} has no profile")
        values = resolve_fixture(prof, dev.dmx_address, rgb=rgb,
                                 brightness=brightness, strobe_hz=strobe_hz)
        self.bus.set_manual(device_id, values)
        return values

    async def blackout_fixture(self, device_id: str) -> None:
        """'Off' on a Live card: force that fixture dark (an override) until
        released. For the fogger, 'Off' means cancel the manual burst."""
        dev = self._require(device_id)
        if dev.kind == DeviceKind.DMX_FOG:
            self.bus.release_manual(device_id)
            return
        prof = self.store.profile_for(dev)
        if prof is None:
            return
        values = {dev.dmx_address + ch.offset: (ch.off_band.mid if ch.off_band else 0)
                  for ch in prof.channels}
        self.bus.set_manual(device_id, values)

    async def release_fixture(self, device_id: str | None = None) -> None:
        """Hand a fixture (or, with None, every fixture) back to the scene."""
        if device_id is not None:
            self._require(device_id)
        self.bus.release_manual(device_id)

    async def fog_burst(self, device_id: str, seconds: float, level: int = 255) -> float:
        """Fog on for a bounded time; returns the capped duration. The bus
        expires the override by itself -- no separate watchdog needed."""
        dev = self._require(device_id)
        prof = self.store.profile_for(dev)
        secs = max(0.0, min(seconds, self.fog_max_seconds))
        fog_off = prof.offset_for(ChannelRole.FOG)
        self.bus.set_manual(device_id, {dev.dmx_address + fog_off: int(level)},
                            expires_at=time.monotonic() + secs)
        return secs

    async def fog_off(self, device_id: str) -> None:
        self.bus.release_manual(device_id)

    async def stop_everything(self) -> None:
        """STOP EVERYTHING: scene layer, held frame and every override cleared."""
        self.bus.blackout()

    def manual_devices(self) -> list[str]:
        return self.bus.manual_devices()

    def _require(self, device_id: str) -> Device:
        dev = self.store.device(device_id)
        if dev is None:
            raise KeyError(device_id)
        return dev
