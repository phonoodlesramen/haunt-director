"""Show Director — unattended ambient rotation + presence-triggered scares.

Armed, it rotates the ambient-tagged scenes on loop. A presence trigger
(GPIO / webhook / manual button) interrupts with a RANDOM scare-tagged scene,
then returns to ambient. A cooldown keeps one guest from chaining scares.

Options (saved in show.json):
  ambient_enabled   off = a sensor-only show: the yard stays dark and quiet
                    between scares instead of rotating ambient scenes.
  sensor_enabled    off = the sensor never fires a scare (it still reads, so
                    you can watch it); the manual button still works.
  presence_hold_s   someone has to stay in range this long before a scare
                    fires (filters a quick pass or a flicker). 0 = instant.

Reliability first: it always returns to ambient, disarm stops everything, and
the fog watchdog (elsewhere) still guarantees fog can never stick on.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("haunt.show")


def _find_rp1_chip():
    """Pi 5 exposes the 40-pin header on the RP1 gpiochip, which is NOT chip 0/4
    (lgpio's default). Find it by label so we open the right one."""
    try:
        import lgpio
    except Exception:
        return None
    for n in range(0, 32):
        try:
            h = lgpio.gpiochip_open(n)
            info = lgpio.gpio_get_chip_info(h)
            lgpio.gpiochip_close(h)
            if "rp1" in str(info[3]).lower():
                return n
        except Exception:
            continue
    return None


def _ensure_pin_factory():
    from gpiozero import Device
    from gpiozero.pins.lgpio import LGPIOFactory
    if isinstance(getattr(Device, "pin_factory", None), LGPIOFactory):
        return
    chip = _find_rp1_chip()
    Device.pin_factory = LGPIOFactory(chip=chip) if chip is not None else LGPIOFactory()


HOLD_MAX_S = 5.0


def _clamp_hold(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v != v:                                  # NaN
        return 0.0
    return round(max(0.0, min(HOLD_MAX_S, v)), 2)


def _next_halloween() -> str:
    """This year's Oct 31, or next year's once it has passed."""
    today = datetime.now().date()
    y = today.year if (today.month, today.day) <= (10, 31) else today.year + 1
    return f"{y}-10-31"


class ShowDirector:
    def __init__(self, player, store, ctrl, config_path):
        self.player = player
        self.store = store
        self.ctrl = ctrl
        self.config_path = Path(config_path)
        self.armed = False
        self.state = "disarmed"           # disarmed|ambient|scare|cooldown|idle
        self.current = None
        self.cooldown_s = 20.0
        self.crossfade_s = 2.0            # global default audio crossfade between scenes
        self.gpio_pin = None
        self.gpio_active_high = True
        self.ambient_enabled = True       # False -> sensor-only: dark & quiet between scares
        self.sensor_enabled = True        # False -> presence never fires (button still does)
        self.presence_hold_s = 0.0        # presence must last this long before firing
        self._hold = None                 # pending hold timer (asyncio TimerHandle)
        self._trigger = asyncio.Event()
        self._task = None
        self._amb_i = 0
        self._last_scare_end = 0.0
        self._last_trigger_at = 0.0
        self._loop = None
        self._gpio = None
        self.schedule_enabled = False
        self.windows = []
        self.lat = None
        self.lng = None
        self._auto_armed = False
        self._sched_task = None
        self._load()
        # the crossfade default applies to manual plays too, so push it to the player
        with contextlib.suppress(Exception):
            self.player.default_crossfade_s = self.crossfade_s

    # ---- persistence (arming is always an explicit act, never auto-restored) ----
    def _load(self):
        with contextlib.suppress(Exception):
            d = json.loads(self.config_path.read_text())
            self.cooldown_s = float(d.get("cooldown_s", self.cooldown_s))
            self.crossfade_s = max(0.0, float(d.get("crossfade_s", self.crossfade_s)))
            self.gpio_pin = d.get("gpio_pin", None)
            self.gpio_active_high = bool(d.get("gpio_active_high", True))
            self.ambient_enabled = bool(d.get("ambient_enabled", True))
            self.sensor_enabled = bool(d.get("sensor_enabled", True))
            self.presence_hold_s = _clamp_hold(d.get("presence_hold_s", 0.0))
            self.schedule_enabled = bool(d.get("schedule_enabled", False))
            self.windows = d.get("windows") or []
            self.lat = d.get("lat", None)
            self.lng = d.get("lng", None)

        if not self.windows:
            self.windows = [
                {"label": "Nightly (dusk)", "enabled": False, "date": "",
                 "start_mode": "dusk", "time": "18:00", "dusk_offset_min": 0, "duration_min": 30},
                {"label": "Halloween night", "enabled": False, "date": _next_halloween(),
                 "start_mode": "dusk", "time": "18:00", "dusk_offset_min": -15, "duration_min": 270},
            ]
        # Default location for dusk schedules: Salem, Massachusetts. Set your own
        # under Show -> Schedule so "dusk" means dusk where you live.
        if self.lat is None: self.lat = 42.52
        if self.lng is None: self.lng = -70.90

    def _save(self):
        with contextlib.suppress(Exception):
            self.config_path.write_text(json.dumps({
                "cooldown_s": self.cooldown_s,
                "crossfade_s": self.crossfade_s,
                "gpio_pin": self.gpio_pin,
                "gpio_active_high": self.gpio_active_high,
                "ambient_enabled": self.ambient_enabled,
                "sensor_enabled": self.sensor_enabled,
                "presence_hold_s": self.presence_hold_s,
                "schedule_enabled": self.schedule_enabled,
                "windows": self.windows,
                "lat": self.lat,
                "lng": self.lng,
            }, indent=2))

    def _pool(self, tag):
        return [s for s in sorted(self.store.scenes.values(),
                                  key=lambda x: (getattr(x, "order", 0), x.name))
                if getattr(s, "tag", "") == tag and getattr(s, "enabled", True)]

    def status(self):
        now = time.monotonic()
        return {
            "armed": self.armed,
            "state": self.state,
            "current": self.current,
            "cooldown_s": self.cooldown_s,
            "crossfade_s": self.crossfade_s,
            "ambient": [s.id for s in self._pool("ambient")],
            "scares": [s.id for s in self._pool("scare")],
            "in_cooldown": self.state != "scare" and (now - self._last_scare_end) < self.cooldown_s,
            "gpio_pin": self.gpio_pin,
            "gpio_active_high": self.gpio_active_high,
            "gpio_ok": self._gpio is not None,
            "presence_active": self.presence_active,
            "ambient_enabled": self.ambient_enabled,
            "sensor_enabled": self.sensor_enabled,
            "presence_hold_s": self.presence_hold_s,
            "hold_pending": self._hold is not None,
            "last_trigger_at": self._last_trigger_at,
            "schedule_enabled": self.schedule_enabled,
            "windows": self.windows,
            "lat": self.lat, "lng": self.lng,
            "sunset_today": self._sunset_str(),
            "active_window": self._active_window_label(),
        }

    def attach_loop(self, loop):
        self._loop = loop

    # ---- control ----
    async def arm(self, on: bool):
        self.armed = bool(on)
        if self.armed:
            # Arming starts the night from a clean rig: drop any Live-card
            # override left over from testing (new ones can still be set).
            with contextlib.suppress(Exception):
                self.ctrl.bus.release_manual()
            if not self.ambient_enabled and (self._task is None or self._task.done()):
                # a sensor-only show starts dark and quiet (no leftover preview)
                with contextlib.suppress(Exception):
                    await self.player.stop()
            self._trigger.clear()
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
        else:
            self._cancel_hold()
            await self._stop_task()
            with contextlib.suppress(Exception):
                await self.player.stop()
            with contextlib.suppress(Exception):
                await self.ctrl.stop_everything()
            self.state = "disarmed"
            self.current = None

    async def _stop_task(self):
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

    def trigger(self):
        self._last_trigger_at = time.time()
        self._trigger.set()

    def set_config(self, cooldown_s=None, gpio_pin=..., gpio_active_high=None, crossfade_s=None,
                   ambient_enabled=None, sensor_enabled=None, presence_hold_s=None):
        if cooldown_s is not None:
            self.cooldown_s = max(0.0, float(cooldown_s))
        if ambient_enabled is not None:
            self.ambient_enabled = bool(ambient_enabled)   # the show loop picks it up within ~1 s
        if sensor_enabled is not None:
            self.sensor_enabled = bool(sensor_enabled)
            if not self.sensor_enabled:
                # stand the sensor down NOW: no pending hold, no latched trigger
                self._cancel_hold()
                self._trigger.clear()
        if presence_hold_s is not None:
            self.presence_hold_s = _clamp_hold(presence_hold_s)
        if crossfade_s is not None:
            self.crossfade_s = max(0.0, min(15.0, float(crossfade_s)))
            with contextlib.suppress(Exception):
                self.player.default_crossfade_s = self.crossfade_s
        pin_changed = False
        if gpio_pin is not ...:
            new = None if gpio_pin in (None, "", -1) else int(gpio_pin)
            if new != self.gpio_pin:
                self.gpio_pin = new
                pin_changed = True
        if gpio_active_high is not None and bool(gpio_active_high) != self.gpio_active_high:
            self.gpio_active_high = bool(gpio_active_high)
            pin_changed = True
        self._save()
        if pin_changed:
            self.start_gpio()

    # ---- schedule / quiet hours (auto-arm windows; "dusk" = local sunset) ----
    def set_schedule(self, enabled=None, lat=None, lng=None, windows=None):
        if enabled is not None:
            self.schedule_enabled = bool(enabled)
        if lat is not None:
            self.lat = float(lat)
        if lng is not None:
            self.lng = float(lng)
        if windows is not None:
            self.windows = list(windows)
        self._save()

    def start_scheduler(self):
        if self._sched_task is None or self._sched_task.done():
            self._sched_task = asyncio.create_task(self._scheduler())

    def _sunset_dt(self, d):
        """Local sunset datetime for date d at self.lat/lng (Almanac algorithm)."""
        if self.lat is None or self.lng is None:
            return None
        try:
            lat, lng, zenith = float(self.lat), float(self.lng), 90.833
            N = d.timetuple().tm_yday
            lngHour = lng / 15.0
            t = N + ((18 - lngHour) / 24.0)
            M = (0.9856 * t) - 3.289
            L = (M + 1.916 * math.sin(math.radians(M)) + 0.020 * math.sin(math.radians(2 * M)) + 282.634) % 360
            RA = math.degrees(math.atan(0.91764 * math.tan(math.radians(L)))) % 360
            RA += (math.floor(L / 90) * 90) - (math.floor(RA / 90) * 90)
            RA /= 15.0
            sinDec = 0.39782 * math.sin(math.radians(L))
            cosDec = math.cos(math.asin(sinDec))
            cosH = (math.cos(math.radians(zenith)) - sinDec * math.sin(math.radians(lat))) / (cosDec * math.cos(math.radians(lat)))
            if cosH < -1 or cosH > 1:
                return None
            H = math.degrees(math.acos(cosH)) / 15.0
            T = H + RA - (0.06571 * t) - 6.622
            UT = (T - lngHour) % 24
            base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            return (base + timedelta(hours=UT)).astimezone()
        except Exception:
            return None

    def _sunset_str(self):
        s = self._sunset_dt(datetime.now().astimezone().date())
        return s.strftime("%H:%M") if s else None

    def _window_start(self, w, now):
        if w.get("start_mode") == "dusk":
            s = self._sunset_dt(now.date())
            return s + timedelta(minutes=float(w.get("dusk_offset_min", 0))) if s else None
        try:
            hh, mm = str(w.get("time", "18:00")).split(":")
            return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except Exception:
            return None

    def _window_active(self, w, now):
        if not w.get("enabled", False):
            return False
        date = (w.get("date") or "").strip()
        if date:
            if now.strftime("%Y-%m-%d") != date:
                return False
        start = self._window_start(w, now)
        if start is None:
            return False
        end = start + timedelta(minutes=float(w.get("duration_min", 30)))
        return start <= now <= end

    def _active_window_label(self):
        now = datetime.now().astimezone()
        for w in self.windows:
            if self._window_active(w, now):
                return w.get("label", "scheduled")
        return None

    async def _scheduler(self):
        while True:
            try:
                now = datetime.now().astimezone()
                want = self.schedule_enabled and any(self._window_active(w, now) for w in self.windows)
                if want and not self.armed:
                    await self.arm(True); self._auto_armed = True
                elif not want and self.armed and self._auto_armed:
                    # a scheduled window ended (or the schedule was turned off): stand down
                    await self.arm(False); self._auto_armed = False
                elif want:
                    self._auto_armed = True
            except Exception:
                pass
            await asyncio.sleep(20)

    # ---- presence GPIO (LD2410 OUT pin: HIGH when a target is present) ----
    def start_gpio(self):
        self._cancel_hold()
        with contextlib.suppress(Exception):
            if self._gpio is not None:
                self._gpio.close()
        self._gpio = None
        if self.gpio_pin is None:
            return
        try:
            from gpiozero import DigitalInputDevice
            _ensure_pin_factory()
            # Pull the line toward "clear": down for a sensor that goes HIGH on
            # presence, up for one that goes LOW. The LD2410 drives its OUT pin
            # both ways, so this changes nothing while it's connected, but a
            # loose or unplugged cable now reads as nobody there. (A floating
            # input tends to read as someone standing there: phantom scares.)
            dev = DigitalInputDevice(self.gpio_pin, pull_up=not bool(self.gpio_active_high),
                                     bounce_time=0.08)
            dev.when_activated = self._on_presence
            dev.when_deactivated = self._on_clear
            self._gpio = dev
            log.info("presence GPIO active on pin %s (active_high=%s)",
                     self.gpio_pin, self.gpio_active_high)
        except Exception as e:
            log.warning("presence GPIO unavailable on pin %s: %s", self.gpio_pin, e)
            self._gpio = None

    def _on_presence(self):
        self._from_gpio(self._presence_started)

    def _on_clear(self):
        self._from_gpio(self._cancel_hold)

    def _from_gpio(self, fn):
        loop = self._loop                      # gpiozero calls back from its own thread
        if loop is not None:
            with contextlib.suppress(RuntimeError):     # loop closed during shutdown
                loop.call_soon_threadsafe(fn)
        else:
            fn()

    def _presence_started(self):
        """Sensor went PRESENT (runs on the event loop)."""
        if not self.sensor_enabled:
            return                             # sensor is switched off: watch, don't fire
        self._cancel_hold()
        if self.presence_hold_s <= 0 or self._loop is None:
            self.trigger()
            return
        self._hold = self._loop.call_later(self.presence_hold_s, self._hold_elapsed)

    def _hold_elapsed(self):
        self._hold = None
        # still there, and still allowed to fire? (re-read the pin rather than
        # trusting that we saw every edge)
        if self.sensor_enabled and self.presence_active:
            self.trigger()

    def _cancel_hold(self):
        h, self._hold = self._hold, None
        if h is not None:
            h.cancel()

    @property
    def presence_active(self):
        """Live LD2410 OUT level as gpiozero reads it: True = target present,
        False = clear, None = no sensor configured/available. active_state
        already accounts for gpio_active_high, so True always means 'present'."""
        g = self._gpio
        if g is None:
            return None
        try:
            return bool(g.is_active)
        except Exception:
            return None

    # ---- the show loop ----
    def _should_scare(self):
        return (self._trigger.is_set()
                and bool(self._pool("scare"))
                and (time.monotonic() - self._last_scare_end) >= self.cooldown_s)

    async def _run(self):
        try:
            while self.armed:
                if self._should_scare():
                    self._trigger.clear()
                    self.state = "scare"
                    sc = random.choice(self._pool("scare"))
                    self.current = sc.id
                    # crossfades over whatever ambient bed is still sounding; the
                    # scare's own audio starts at full so it still hits hard.
                    with contextlib.suppress(Exception):
                        await self.player.play(sc, loop=False, sustain_on_end=True)
                    while self.armed and self.player.is_playing:
                        await asyncio.sleep(0.1)
                    self._last_scare_end = time.monotonic()
                    self._trigger.clear()            # drop triggers that arrived mid-scare
                    self.state = "cooldown"
                    continue
                amb = self._pool("ambient") if self.ambient_enabled else []
                if not amb:
                    # Nothing to hand off to (sensor-only show, or no ambient
                    # scenes): wind down the scene WE started, once, so its
                    # sustained audio fades out and the rig goes dark. A scene
                    # the user started by hand is left alone (settle checks),
                    # and Live-card overrides are untouched.
                    if self.current is not None:
                        with contextlib.suppress(Exception):
                            await self.player.settle(self.current)
                        self.current = None
                    self.state = "idle"
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._trigger.wait(), timeout=1.0)
                    continue
                self.state = "ambient"
                sc = amb[self._amb_i % len(amb)]
                self._amb_i += 1
                self.current = sc.id
                # sustain_on_end keeps this bed alive past the scene's end so the
                # NEXT scene's play() crossfades it out instead of a hard cut.
                with contextlib.suppress(Exception):
                    await self.player.play(sc, loop=False, sustain_on_end=True)
                while (self.armed and self.ambient_enabled and self.player.is_playing
                       and not self._should_scare()):
                    await asyncio.sleep(0.1)
                # If a scare is due, DON'T stop here — fall through so the scare's
                # play() crossfades over this still-playing ambient bed.
        except asyncio.CancelledError:
            pass
        finally:
            with contextlib.suppress(Exception):
                await self.player.stop()
