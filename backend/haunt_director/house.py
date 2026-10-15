"""House lights: the yard lit for the nights there's no show.

Lights only. House lights never touch the fogger and never play a sound; they
drive only the DMX fixtures, as their own layer on the output bus underneath
any scene (see output.py). Each light glides slowly through a short list of
colours you pick (one colour holds steady), slightly out of step with the
others, so the house always looks a little different without ever "doing"
anything.

When they run:
  mode "off"       never
  mode "on"        now, until you switch them off
  mode "schedule"  inside any enabled window: from dusk (plus or minus an
                   offset) or a clock time, to an end time, on the days picked.
                   A window may run past midnight.
and in every case only while the show is disarmed and nothing is playing: the
show always owns the yard, and house lights come back when it lets go.

STOP EVERYTHING turns them off until the next window starts (or, in mode "on",
until you switch them on again), so the big red button always leaves the yard
dark.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import threading
import time
from datetime import date as _date, datetime, timedelta
from pathlib import Path

from .domain.devices import DeviceKind
from .domain.resolve import resolve_fixture

log = logging.getLogger("haunt.house")

MODES = ("off", "schedule", "on")
MAX_COLORS = 4
HOLD_MIN, HOLD_MAX = 1.0, 60.0          # minutes each colour roughly lasts
GLIDE = 0.6                             # share of each step spent gliding (the rest holds)
FADE_S = 4.0                            # fade in / out when house lights start or stop
TICK_S = 0.5
DEFAULT_COLORS = ["#ff7a1a", "#7a2cff"]  # ember orange <-> violet
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


# ---------------- pure helpers (web/house.js mirrors these; keep them in step) ----------------
def hex_rgb(h):
    h = str(h or "#000000").lstrip("#")
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def _round(x):
    return int(math.floor(x + 0.5))      # like JavaScript's Math.round (Python's round() is banker's)


def phase_for(index):
    """Where light number `index` starts in its colour cycle, in steps. The
    golden-ratio stagger keeps neighbours from ever changing together."""
    return (index * 0.618034) % 1.0


def drift_rgb(colors, t, hold_min, phase=0.0):
    """The colour a light shows at wall-clock time t (seconds). Each colour
    holds for (1 - GLIDE) of a step, then glides smoothly into the next; the
    list loops. One colour: steady."""
    cols = [hex_rgb(c) for c in (colors or [])][:MAX_COLORS]
    if not cols:
        return (0, 0, 0)
    if len(cols) == 1:
        return cols[0]
    n = len(cols)
    step = max(60.0, float(hold_min) * 60.0)
    pos = (t / step + phase) % n
    k = int(math.floor(pos))
    f = pos - k
    hold = 1.0 - GLIDE
    g = 0.0 if f < hold else (f - hold) / GLIDE
    g = 0.5 - 0.5 * math.cos(math.pi * g)
    a, b = cols[k % n], cols[(k + 1) % n]
    return tuple(_round(a[i] + (b[i] - a[i]) * g) for i in range(3))


def _hm(s, default):
    try:
        hh, mm = str(s).split(":")[:2]
        hh, mm = int(hh), int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh, mm
    except Exception:
        pass
    return default


def window_bounds(w, day, sunset_fn):
    """(start, end) of window w when it starts on `day`, or None if it doesn't
    run that day. The end is the next time the end clock comes round after the
    start, so 'dusk to 1:00' runs past midnight."""
    days = w.get("days")
    if isinstance(days, list) and days and day.weekday() not in days:
        return None
    if (w.get("start_mode") or "dusk") == "dusk":
        s = sunset_fn(day)
        if s is None:
            return None
        start = s + timedelta(minutes=float(w.get("dusk_offset_min") or 0))
    else:
        hh, mm = _hm(w.get("time"), (18, 0))
        start = datetime(day.year, day.month, day.day, hh, mm).astimezone()
    eh, em = _hm(w.get("end"), (23, 0))
    sd = start.date()
    end = datetime(sd.year, sd.month, sd.day, eh, em).astimezone()
    if end <= start:                        # past midnight: the end is on the next day
        nd = sd + timedelta(days=1)
        end = datetime(nd.year, nd.month, nd.day, eh, em).astimezone()
    return start, end


def active_window(windows, now, sunset_fn):
    """(window, start, end) for the enabled window running at `now`, else None.
    Looks at yesterday's start too, for windows that run past midnight."""
    for w in windows or []:
        if not w.get("enabled", True):
            continue
        for back in (0, 1):
            b = window_bounds(w, (now - timedelta(days=back)).date(), sunset_fn)
            if b and b[0] <= now < b[1]:
                return w, b[0], b[1]
    return None


def next_start(windows, now, sunset_fn, days_ahead=8):
    best = None
    for w in windows or []:
        if not w.get("enabled", True):
            continue
        for ahead in range(days_ahead):
            b = window_bounds(w, (now + timedelta(days=ahead)).date(), sunset_fn)
            if b and b[0] > now and (best is None or b[0] < best):
                best = b[0]
                break
    return best


def clean_config(d, light_ids):
    """Validate and normalise a whole house-lights config."""
    d = d if isinstance(d, dict) else {}
    out = {"mode": d.get("mode") if d.get("mode") in MODES else "off"}
    try:
        out["brightness"] = max(0.0, min(1.0, float(d.get("brightness", 0.8))))
    except (TypeError, ValueError):
        out["brightness"] = 0.8
    try:
        out["hold_min"] = max(HOLD_MIN, min(HOLD_MAX, float(d.get("hold_min", 8.0))))
    except (TypeError, ValueError):
        out["hold_min"] = 8.0
    src = d.get("lights") if isinstance(d.get("lights"), dict) else {}
    lights = {}
    for lid in light_ids:
        e = src.get(lid) if isinstance(src.get(lid), dict) else {}
        cols = [c.lower() for c in (e.get("colors") or []) if isinstance(c, str) and _HEX.match(c)][:MAX_COLORS]
        lights[lid] = {"on": bool(e.get("on", True)), "colors": cols or list(DEFAULT_COLORS)}
    out["lights"] = lights
    wins = []
    for w in (d.get("windows") if isinstance(d.get("windows"), list) else [])[:12]:
        if not isinstance(w, dict):
            continue
        days = sorted({int(x) for x in (w.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})
        sh, sm = _hm(w.get("time"), (18, 0))
        eh, em = _hm(w.get("end"), (23, 0))
        try:
            off = max(-180, min(180, int(float(w.get("dusk_offset_min") or 0))))
        except (TypeError, ValueError):
            off = 0
        wins.append({"label": str(w.get("label") or "Evenings")[:40],
                     "enabled": bool(w.get("enabled", True)),
                     "start_mode": "clock" if w.get("start_mode") == "clock" else "dusk",
                     "dusk_offset_min": off, "time": f"{sh:02d}:{sm:02d}", "end": f"{eh:02d}:{em:02d}",
                     "days": days or list(range(7))})
    out["windows"] = wins
    return out


def default_config(light_ids):
    return clean_config({"mode": "off", "windows": [
        {"label": "Evenings", "enabled": True, "start_mode": "dusk", "dusk_offset_min": 0,
         "time": "18:00", "end": "23:00", "days": list(range(7))}]}, light_ids)


# ---------------- the controller ----------------
class HouseLights:
    def __init__(self, store, bus, player, director, path):
        self.store, self.bus, self.player, self.director = store, bus, player, director
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            raw = json.loads(self.path.read_text())
            self.cfg = clean_config(raw, self._light_ids())
        except Exception:
            self.cfg = default_config(self._light_ids())
        self.active = False            # our layer is on the bus
        self.reason = "off"
        self._ramp = (0.0, 0.0, 0.0)   # (from level, to level, start monotonic)
        self._off_at = None            # monotonic time the fade-out finishes
        self._snooze = None            # the run STOP EVERYTHING ended: stay off until it changes
        self._task = None
        self._devs = ()                # (id, dmx address, profile) per light, for the output thread
        self._refresh_devs()

    # ---- config ----
    def _lights(self):
        out = []
        for dev in sorted(self.store.devices.values(), key=lambda d: (d.dmx_address or 0, d.id)):
            if dev.kind == DeviceKind.DMX_FIXTURE and getattr(dev, "enabled", True) and dev.dmx_address is not None:
                out.append(dev)
        return out

    def _light_ids(self):
        return [d.id for d in self._lights()]

    def _refresh_devs(self):
        devs = []
        for dev in self._lights():
            with contextlib.suppress(Exception):
                prof = self.store.profile_for(dev)
                if prof is not None:
                    devs.append((dev.id, dev.dmx_address, prof))
        self._devs = tuple(devs)

    def refresh_devices(self):
        """The rig changed (rig editor): pick up new lights, drop removed ones.
        A new light that follows another starts with that light's colors."""
        ids = self._light_ids()
        with self._lock:
            old = self.cfg.get("lights") or {}
            seeded = dict(old)
            for dev in self._lights():
                if dev.id not in seeded and dev.follows and dev.follows in old:
                    seeded[dev.id] = dict(old[dev.follows])
            new = clean_config({**self.cfg, "lights": seeded}, ids)
            changed = new != self.cfg
            self.cfg = new
            if changed:
                self._save()
        self._refresh_devs()

    def _save(self):
        with contextlib.suppress(Exception):
            self.path.write_text(json.dumps(self.cfg, indent=2))

    def set_config(self, patch):
        merged = dict(self.cfg)
        merged.update({k: v for k, v in (patch or {}).items() if k in ("mode", "brightness", "hold_min", "lights", "windows")})
        new = clean_config(merged, self._light_ids())
        with self._lock:
            if new["mode"] != self.cfg.get("mode"):
                self._snooze = None
            self.cfg = new
        self._save()
        self.tick()
        return self.status()

    def set_mode(self, mode):
        """An explicit Off / Schedule / On from the card. Choosing one (even the
        current one) clears a STOP EVERYTHING pause."""
        self._snooze = None
        return self.set_config({"mode": mode})

    def snooze(self):
        """STOP EVERYTHING: off now, and stay off until the next window (or, in
        mode 'on', until switched on again)."""
        key = self._run_key(datetime.now().astimezone())
        self._snooze = key if key is not None else "now"
        self._off(immediate=True)
        self.reason = "stopped"

    # ---- deciding ----
    def _sunset(self, day):
        fn = getattr(self.director, "_sunset_dt", None)
        return fn(day) if fn else None

    def _run_key(self, now):
        mode = self.cfg.get("mode")
        if mode == "on":
            return "on"
        if mode == "schedule":
            aw = active_window(self.cfg.get("windows"), now, self._sunset)
            if aw:
                return f"{aw[0].get('label')}@{aw[1].isoformat()}"
        return None

    def wanted(self, now=None):
        now = now or datetime.now().astimezone()
        mode = self.cfg.get("mode")
        if mode == "off":
            return False, "off"
        key = self._run_key(now)
        if key is None:
            if self._snooze not in (None, "on"):
                self._snooze = None                 # the snoozed window is over
            return False, "waiting"
        if self._snooze is not None and self._snooze in (key, "now"):
            if self._snooze == "now":
                self._snooze = key
            return False, "stopped"
        if getattr(self.director, "armed", False):
            return False, "show"
        if getattr(self.player, "is_playing", False):
            return False, "playing"
        return True, "on" if mode == "on" else "schedule"

    def tick(self):
        """One decision: turn the layer on (fade in), off (fade out, or at once
        if a scene or the show took over), or leave it."""
        self._refresh_devs()
        try:
            want, why = self.wanted()
        except Exception as e:
            log.warning("house lights: %s", e)
            want, why = False, "error"
        self.reason = why
        now = time.monotonic()
        if want:
            self._off_at = None
            if not self.active or self._ramp[1] < 1.0:
                if not self.active:
                    with contextlib.suppress(Exception):
                        self.bus.clear_held_if_idle()   # a finished test scene's last look
                self._ramp = (self._level(now) if self.active else 0.0, 1.0, now)
                self.active = True
                self.bus.set_house(self._source)
        elif self.active:
            if why in ("show", "playing", "stopped", "error"):
                self._off(immediate=True)
            elif self._off_at is None:
                self._ramp = (self._level(now), 0.0, now)
                self._off_at = now + FADE_S
            elif now >= self._off_at:
                self._off(immediate=True)

    def _off(self, immediate=True):
        self.active = False
        self._off_at = None
        self._ramp = (0.0, 0.0, time.monotonic())
        with contextlib.suppress(Exception):
            self.bus.set_house(None)

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._off()

    async def _loop(self):
        while True:
            self.tick()
            await asyncio.sleep(TICK_S)

    # ---- the layer (called from the output thread every frame) ----
    def _level(self, now):
        a, b, t0 = self._ramp
        f = min(1.0, max(0.0, (now - t0) / FADE_S))
        return a + (b - a) * f

    def colors_now(self, t=None):
        """{light id: (rgb, brightness 0..255)} at wall time t, at full level."""
        t = time.time() if t is None else t
        cfg = self.cfg                                   # replaced whole on save: a consistent snapshot
        out = {}
        for i, (dev_id, _addr, _prof) in enumerate(self._devs):
            e = cfg["lights"].get(dev_id) or {"on": True, "colors": DEFAULT_COLORS}
            if not e.get("on", True):
                out[dev_id] = ((0, 0, 0), 0)
                continue
            rgb = drift_rgb(e.get("colors"), t, cfg.get("hold_min", 8.0), phase_for(i))
            out[dev_id] = (rgb, _round(255 * cfg.get("brightness", 0.8)))
        return out

    def _source(self, now):
        """The bus calls this every frame while house lights are on. Lights
        only: fixtures, never the fogger, never a strobe."""
        level = self._level(now)
        cols = self.colors_now()
        chans = {}
        for dev_id, addr, prof in self._devs:
            rgb, bri = cols.get(dev_id, ((0, 0, 0), 0))
            chans.update(resolve_fixture(prof, addr, rgb=rgb, brightness=_round(bri * level)))
        return chans

    # ---- status ----
    def status(self):
        now = datetime.now().astimezone()
        aw = active_window(self.cfg.get("windows"), now, self._sunset)
        nxt = None if aw else next_start(self.cfg.get("windows"), now, self._sunset)
        cols = {k: {"hex": "#%02x%02x%02x" % rgb, "brightness": bri}
                for k, (rgb, bri) in self.colors_now().items()}
        return {**self.cfg,
                "status": {"active": self.active, "reason": self.reason,
                           "level": round(self._level(time.monotonic()), 3),
                           "window": aw[0].get("label") if aw else None,
                           "until": aw[2].strftime("%H:%M") if aw else None,
                           "next_on": nxt.strftime("%a %H:%M") if nxt else None,
                           "sunset_today": (self._sunset(now.date()).strftime("%H:%M")
                                            if self._sunset(now.date()) else None),
                           "now": now.strftime("%H:%M"),
                           "colors": cols},
                "light_names": {d.id: d.name for d in self._lights()},
                "light_order": [d[0] for d in self._devs]}
