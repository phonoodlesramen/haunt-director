"""The single owner of everything the lights and the fogger put out.

Why this exists (Sept 2026 review). Light output used to be written from four
places (scene player, Live cards, fog burst + watchdog, Stop), each sending small
HTTP "range" writes into FPP. FPP 9.5 stores every distinct range it is sent in
an append-only list and re-applies that list oldest-first every output frame, so
the player's merged ranges -- whose boundaries shifted frame to frame -- left
stale entries overriding fresh values: fixtures stuck dark, on the wrong colour,
or strobing, until a hard Stop cleared the list. And the writer ran on the web
server's event loop, so any stall there (audio loads, requests) froze the lights.

Now there is ONE writer: a dedicated thread with a fixed-deadline frame clock
that composes a complete frame every tick from layers (highest wins):

    1. safety   fog guard: fog output disabled -> fog channels forced to 0
    2. manual   Live-card / fog-burst overrides, per device, optional expiry
    3. scene    the playing scene (evaluated HERE, in this thread), or the last
                frame held after a scene ends naturally (with fog + strobe off)
    4. house    house lights (house.py): lights only, used only when there is
                no scene and no held frame
    5. idle     zeros

and hands it to a Transport that owns the wire format:

    DdpTransport   one UDP datagram per frame into FPP's DDP input. Atomic across
                   fixtures, PUSH makes fppd output it immediately, and FPP's
                   input timeout drops our data if the app dies (dead-man switch).
    RestTransport  FPP overlay ranges, ONE CHANNEL PER RANGE, never merged, so
                   FPP's list can never hold overlapping entries.

Nothing else in the app may write to FPP.
"""
from __future__ import annotations

import collections
import http.client
import json
import logging
import socket
import threading
import time
from typing import Callable
from urllib.parse import urlparse

from .domain.devices import DeviceKind
from .domain.profiles import ChannelRole

log = logging.getLogger("haunt.output")

FPS = 25          # scene-engine frame rate (the editor simulator assumes 25)
NCH = 64          # channels in the output frame (DMX 1..64; the overlay model spans 64)

SceneSource = Callable[[float], "dict[int, int]"]


class TransportError(RuntimeError):
    """A write to FPP failed."""


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------
class FppHttp:
    """Minimal keep-alive HTTP client for FPP's overlay API. Synchronous on
    purpose: it is only ever used from the output thread (and its start/stop)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 80, prefix: str = "/api",
                 timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.prefix = prefix.rstrip("/")
        self._conn: http.client.HTTPConnection | None = None

    def put(self, path: str, body: dict) -> dict:
        data = json.dumps(body)
        for attempt in (0, 1):                 # one reconnect: Apache drops idle keep-alives
            try:
                if self._conn is None:
                    self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                self._conn.request("PUT", self.prefix + path, body=data,
                                   headers={"Content-Type": "application/json"})
                resp = self._conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException) as e:
                self.close()
                if attempt:
                    raise TransportError(f"PUT {path} failed: {e}") from e
                continue
            if resp.status != 200:
                raise TransportError(f"PUT {path} -> HTTP {resp.status}")
            try:
                j = json.loads(raw or b"{}")
            except ValueError:
                j = {}
            if isinstance(j, dict) and j.get("Status") not in (None, "OK"):
                raise TransportError(f"PUT {path} -> {j}")
            return j if isinstance(j, dict) else {}
        return {}

    def get(self, path: str) -> dict:
        for attempt in (0, 1):
            try:
                if self._conn is None:
                    self._conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                self._conn.request("GET", self.prefix + path)
                resp = self._conn.getresponse()
                raw = resp.read()
            except (OSError, http.client.HTTPException) as e:
                self.close()
                if attempt:
                    raise TransportError(f"GET {path} failed: {e}") from e
                continue
            if resp.status != 200:
                raise TransportError(f"GET {path} -> HTTP {resp.status}")
            try:
                j = json.loads(raw or b"{}")
            except ValueError:
                j = {}
            return j if isinstance(j, dict) else {}
        return {}

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class RestTransport:
    """FPP overlay-range writes, made safe for FPP 9.5's append-only range list.

    Every channel is written as its own single-channel range (N-N): FPP keeps
    exactly one list entry per channel, nothing can overlap, and list order stops
    mattering. Channels that share a value travel in ONE request as a comma list
    (FPP parses "1,5,9" as three single-channel ranges). Diff against what is
    CONFIRMED on the wire -- a failed PUT is simply re-sent next tick -- plus a
    full re-assert every KEYFRAME_S so anything dropped self-heals."""

    name = "rest"
    KEYFRAME_S = 0.5
    REENABLE_S = 2.0

    def __init__(self, http: FppHttp, model: str):
        self.http = http
        self.model = model
        self._confirmed: dict[int, int] = {}
        self._next_key = 0.0
        self._next_enable = 0.0

    def open(self) -> None:
        # Purge whatever an older build left in FPP's range list (overlapping,
        # stale entries), then make sure the overlay model is on so ranges output.
        self.http.put("/overlays/range", {"deleteAll": True})
        self._confirmed.clear()
        self._next_key = 0.0
        self._enable()

    def _enable(self) -> None:
        self.http.put(f"/overlays/model/{self.model}/state", {"State": 1})
        self._next_enable = time.monotonic() + self.REENABLE_S

    def send(self, frame: bytes, now: float) -> int:
        if now >= self._next_enable:
            self._enable()                        # survives an fppd restart
        if now >= self._next_key:
            todo = {c: frame[c - 1] for c in range(1, len(frame) + 1)}
            self._next_key = now + self.KEYFRAME_S
        else:
            todo = {c: frame[c - 1] for c in range(1, len(frame) + 1)
                    if self._confirmed.get(c) != frame[c - 1]}
        if not todo:
            return 0
        groups: dict[int, list[int]] = collections.defaultdict(list)
        for c, v in todo.items():
            groups[v].append(c)
        sent = 0
        for v in sorted(groups):
            chans = sorted(groups[v])
            self.http.put("/overlays/range/" + ",".join(map(str, chans)), {"Value": int(v)})
            for c in chans:                       # confirmed only after the PUT succeeded
                self._confirmed[c] = v
            sent += 1
        return sent

    def close(self, zero_frame: bytes | None = None) -> None:
        if zero_frame is not None:
            try:
                self._next_key = 0.0
                self.send(zero_frame, time.monotonic())
            except Exception as e:
                log.warning("REST close: zero frame failed: %s", e)
        self.http.close()


class DdpTransport:
    """FPP's native DDP input (UDP 4048): one datagram per frame with every
    channel, so all fixtures change together. Header per FPP 9.5
    src/e131bridge.cpp Bridge_StoreDDPData: byte0 flags (0x40 v1 | 0x01 PUSH),
    byte1 sequence (1..15), byte3 destination id 1, bytes4-7 big-endian 0-based
    channel offset, bytes8-9 big-endian length, data from byte 10.

    FPP applies overlay models and ranges AFTER DDP data (Sequence.cpp
    ProcessSequenceData), so open() clears all ranges and disables the overlay
    model, and re-checks it periodically. It READS the model state first and
    only disables an active model: in FPP 9.5 modelStateChanged() treats any
    change from state 0 as "enabling", so re-sending State:0 to an already
    disabled model would put it back on the active list."""

    name = "ddp"
    REASSERT_S = 5.0

    def __init__(self, http: FppHttp, model: str, host: str = "127.0.0.1", port: int = 4048):
        self.http = http
        self.model = model
        self.addr = (host, port)
        self._sock: socket.socket | None = None
        self._seq = 0
        self._next_reassert = 0.0

    def _model_off(self) -> None:
        info = self.http.get(f"/overlays/model/{self.model}")
        if info.get("isActive"):
            self.http.put(f"/overlays/model/{self.model}/state", {"State": 0})

    def open(self) -> None:
        self.http.put("/overlays/range", {"deleteAll": True})
        self._model_off()
        self._next_reassert = time.monotonic() + self.REASSERT_S
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    @staticmethod
    def packet(frame: bytes, seq: int, offset: int = 0) -> bytes:
        return (bytes((0x41, seq & 0x0F, 0x00, 0x01))
                + int(offset).to_bytes(4, "big")
                + len(frame).to_bytes(2, "big")
                + bytes(frame))

    def send(self, frame: bytes, now: float) -> int:
        if now >= self._next_reassert:
            self._next_reassert = now + self.REASSERT_S
            try:                                   # guard only; never fatal to the frame
                self._model_off()
            except Exception as e:
                log.debug("DDP model-off reassert failed: %s", e)
        self._seq = self._seq % 15 + 1
        assert self._sock is not None
        self._sock.sendto(self.packet(frame, self._seq), self.addr)
        return 1

    def close(self, zero_frame: bytes | None = None) -> None:
        if self._sock is not None:
            if zero_frame is not None:
                for _ in range(3):
                    try:
                        self._seq = self._seq % 15 + 1
                        self._sock.sendto(self.packet(zero_frame, self._seq), self.addr)
                    except OSError:
                        pass
            self._sock.close()
            self._sock = None
        self.http.close()


def make_transport(settings):
    u = urlparse(settings.fpp_base_url)
    http = FppHttp(u.hostname or "127.0.0.1", u.port or 80, "/api", timeout=2.0)
    if str(getattr(settings, "output_transport", "rest")).lower() == "ddp":
        return DdpTransport(http, settings.overlay_model_name,
                            getattr(settings, "ddp_host", "127.0.0.1"),
                            int(getattr(settings, "ddp_port", 4048)))
    return RestTransport(http, settings.overlay_model_name)


# --------------------------------------------------------------------------
# the bus
# --------------------------------------------------------------------------
class OutputBus:
    """One thread, one clock, one complete frame per tick. Every public method
    is thread-safe and non-blocking (safe to call from the asyncio loop)."""

    def __init__(self, store, transport, fps: int = FPS, nch: int = NCH,
                 strobe_clear_frames: int = 0):
        self.store = store
        self.transport = transport
        self.fps = max(1, int(fps))
        self.period = 1.0 / self.fps
        self.nch = nch
        self.strobe_clear_frames = max(0, int(strobe_clear_frames))
        self.fog_enabled = True

        self._lock = threading.RLock()
        self._source: SceneSource | None = None
        self._held: dict[int, int] = {}
        self._last_scene: dict[int, int] = {}
        self._manual: dict[str, tuple[dict[int, int], float | None]] = {}
        self._house: SceneSource | None = None

        self._fog_chans: set[int] = set()
        self._strobe_fx: dict[int, list[int]] = {}
        self._strobe_off: dict[int, int] = {}
        self.refresh_devices()
        self._sc_hold: dict[int, int] = {}
        self._sc_prev: dict[int, int] = {}

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = False
        self._next_open = 0.0

        self.frames = 0
        self.send_errors = 0
        self.overruns = 0
        self.max_tick_ms = 0.0
        self.last_error: str | None = None
        self.last_tick = 0.0
        self._ticks = collections.deque(maxlen=self.fps * 2)
        self._tick_ms = collections.deque(maxlen=self.fps * 2)
        self._err_logged: dict[str, float] = {}
        self.last_frame = bytes(nch)

    # ---- channel block ----
    def ensure_channels(self, n: int) -> None:
        """Grow the frame to cover DMX 1..n (the rig editor added a device past
        the end). Never shrinks while running: channels a removed device used
        simply go to zero on every frame, which is what we want anyway."""
        n = max(1, min(512, int(n)))
        with self._lock:
            if n > self.nch:
                self.nch = n

    # ---- device map (fog guard, strobe channels) ----
    def refresh_devices(self) -> None:
        fog, strobe, soff = set(), {}, {}
        for dev in self.store.devices.values():
            if dev.dmx_address is None:
                continue
            prof = self.store.profile_for(dev)
            if prof is None:
                continue
            if dev.kind == DeviceKind.DMX_FOG:
                off = prof.offset_for(ChannelRole.FOG)
                if off is not None:
                    fog.add(dev.dmx_address + off)          # guard applies even if disabled
            elif dev.kind == DeviceKind.DMX_FIXTURE:
                ch = prof.channel(ChannelRole.STROBE)
                if ch is not None:
                    sch = dev.dmx_address + ch.offset
                    strobe[sch] = [dev.dmx_address + c.offset for c in prof.channels]
                    soff[sch] = ch.off_band.mid if ch.off_band else 0
        with self._lock:
            self._fog_chans, self._strobe_fx, self._strobe_off = fog, strobe, soff

    # ---- scene layer ----
    def set_scene(self, source: SceneSource) -> None:
        """Make `source(now)` the scene layer. Replaces any previous source or
        held frame atomically -- a crossfade handoff has no dark gap."""
        with self._lock:
            self._source = source
            self._held = {}
            self._sc_hold.clear()

    def end_scene(self, hold: bool = True) -> None:
        """Scene over. hold=True keeps its last frame on the rig (with every fog
        and strobe channel off); hold=False drops the scene layer entirely."""
        if not hold:
            with self._lock:
                self._held = {}
                self._source = None
            return
        with self._lock:
            src = self._source
        if src is None:
            return                                  # already ended; keep what is held
        # Capture the scene's actual final frame now (not the output thread's
        # last tick, which could be stale if that thread was ever delayed).
        try:
            final = dict(src(time.monotonic()) or {})
        except Exception as e:
            self._note_error("scene", e)
            final = dict(self._last_scene)
        for c in self._fog_chans:
            final[c] = 0
        for sch, v in self._strobe_off.items():
            if sch in final:
                final[sch] = v
        with self._lock:
            if self._source is src:                 # nobody swapped in a new scene meanwhile
                self._held = final
                self._source = None

    # ---- house layer ----
    def set_house(self, source: SceneSource | None) -> None:
        """House lights underneath everything: shown only when no scene is
        playing and no frame is held. None turns the layer off."""
        with self._lock:
            self._house = source

    def clear_held_if_idle(self) -> None:
        """Drop a held frame (a finished scene's last look) unless a scene is
        playing, so house lights can show."""
        with self._lock:
            if self._source is None:
                self._held = {}

    # ---- manual layer ----
    def set_manual(self, device_id: str, values: dict[int, int],
                   expires_at: float | None = None) -> None:
        with self._lock:
            self._manual[device_id] = (dict(values), expires_at)

    def release_manual(self, device_id: str | None = None) -> None:
        with self._lock:
            if device_id is None:
                self._manual.clear()
            else:
                self._manual.pop(device_id, None)

    def manual_devices(self) -> list[str]:
        now = time.monotonic()
        with self._lock:
            return sorted(d for d, (_, exp) in self._manual.items() if exp is None or now < exp)

    # ---- safety ----
    def blackout(self) -> None:
        """STOP EVERYTHING: drop scene, held frame and every manual override."""
        with self._lock:
            self._source = None
            self._held = {}
            self._last_scene = {}
            self._manual.clear()
            self._sc_hold.clear()

    # ---- composition (pure w.r.t. the transport; called by the thread) ----
    def compose(self, now: float) -> bytes:
        with self._lock:
            src = self._source
            chans = self._held
            manual = list(self._manual.items())
            fog_chans = self._fog_chans
            house = self._house
        if src is None and not chans and house is not None:
            try:
                chans = house(now) or {}
            except Exception as e:                  # house lights must never kill output either
                self._note_error("house", e)
                chans = {}
        if src is not None:
            try:
                chans = src(now) or {}
                self._last_scene = chans
            except Exception as e:                  # a bad scene must never kill output
                self._note_error("scene", e)
                chans = self._last_scene
            if self.strobe_clear_frames:
                chans = self._strobe_clear(chans)
        nch = self.nch
        frame = bytearray(nch)
        for c, v in chans.items():
            if 1 <= c <= nch:
                frame[c - 1] = max(0, min(255, int(v)))
        expired = []
        for dev_id, entry in manual:
            vals, exp = entry
            if exp is not None and now >= exp:
                expired.append((dev_id, entry))
                continue
            for c, v in vals.items():
                if 1 <= c <= nch:
                    frame[c - 1] = max(0, min(255, int(v)))
        if expired:
            with self._lock:
                for dev_id, entry in expired:
                    if self._manual.get(dev_id) is entry:   # not replaced meanwhile
                        del self._manual[dev_id]
        if not self.fog_enabled:
            for c in fog_chans:
                if 1 <= c <= nch:
                    frame[c - 1] = 0
        return bytes(frame)

    def _strobe_clear(self, chans: dict[int, int]) -> dict[int, int]:
        """Legacy strobe-latch workaround (off by default). Kept as an escape
        hatch in case a fixture really latches: after a strobe ends, hold that
        fixture dark for N frames unless a new strobe starts."""
        out = dict(chans)
        for sch, fx in self._strobe_fx.items():
            cur = chans.get(sch, 0)
            if self._sc_prev.get(sch, 0) > 0 and cur == 0:
                self._sc_hold[sch] = self.strobe_clear_frames
            self._sc_prev[sch] = cur
            if self._sc_hold.get(sch, 0) > 0:
                if cur > 0:
                    self._sc_hold[sch] = 0
                    continue
                self._sc_hold[sch] -= 1
                for c in fx:
                    out[c] = 0
        return out

    # ---- thread ----
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="output-bus", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        next_t = time.monotonic()
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                if not self._ready and t0 >= self._next_open:
                    try:
                        self.transport.open()
                        self._ready = True
                        log.info("output bus ready (transport=%s, %d fps)",
                                 self.transport.name, self.fps)
                    except Exception as e:
                        self._next_open = t0 + 2.0
                        self._note_error("open", e)
                frame = self.compose(t0)
                if self._ready:
                    self.transport.send(frame, t0)
                self.last_frame = frame
                self.frames += 1
            except Exception as e:
                self.send_errors += 1
                self._note_error("send", e)
            ms = (time.monotonic() - t0) * 1000.0
            self._tick_ms.append(ms)
            self.max_tick_ms = max(self.max_tick_ms, ms)
            self._ticks.append(t0)
            self.last_tick = t0
            next_t += self.period
            delay = next_t - time.monotonic()
            if delay < -self.period:        # fell a whole frame behind: resync, never burst
                self.overruns += 1
                next_t = time.monotonic()
            elif delay > 0:
                self._stop.wait(delay)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the clock and leave the rig dark (fog guaranteed off)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        try:
            self.transport.close(bytes(self.nch) if self._ready else None)
        except Exception as e:
            log.warning("output bus close failed: %s", e)
        self._ready = False

    def _note_error(self, where: str, e: Exception) -> None:
        self.last_error = f"{where}: {e}"
        now = time.monotonic()
        if now - self._err_logged.get(where, -1e9) >= 30.0:   # rate-limit the log
            self._err_logged[where] = now
            log.warning("output bus %s error: %s", where, e)

    def stats(self) -> dict:
        now = time.monotonic()
        t = list(self._ticks)
        fps = (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0.0
        recent = list(self._tick_ms)
        with self._lock:
            scene_active = self._source is not None
            holding = bool(self._held)
        return {
            "transport": self.transport.name,
            "ready": self._ready,
            "alive": self.alive,
            "fps": round(fps, 1),
            "target_fps": self.fps,
            "frames": self.frames,
            "send_errors": self.send_errors,
            "overruns": self.overruns,
            "tick_ms_recent_max": round(max(recent), 1) if recent else None,
            "tick_ms_max": round(self.max_tick_ms, 1),
            "stale_s": round(now - self.last_tick, 2) if self.last_tick else None,
            "last_error": self.last_error,
            "scene_active": scene_active,
            "holding_last_frame": holding,
            "manual": self.manual_devices(),
            "house": self._house is not None,
            "fog_enabled": self.fog_enabled,
        }
