"""Show options: sensor-only shows (dark and quiet between scares), a sensor
on/off switch that keeps the pin, and a presence hold time. All three persist,
and none of them can leave a scare's sound or look stuck on the rig."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from haunt_director.output import OutputBus
from haunt_director.player import ShowPlayer
from haunt_director.show import ShowDirector, HOLD_MAX_S
from haunt_director.store import Store


# ---------- fakes for the show loop ----------
class Sc:
    def __init__(self, id, tag):
        self.id, self.name, self.tag, self.enabled, self.order = id, id, tag, True, 0


class FakeStore:
    def __init__(self, *scenes):
        self.scenes = {s.id: s for s in scenes}


class FakePlayer:
    """Plays a scene for `dur` seconds; records plays, stops and settles."""
    def __init__(self, dur=0.2):
        self.dur, self.plays, self.settled, self.stops = dur, [], [], 0
        self.default_crossfade_s = 2.0
        self._until = 0.0

    @property
    def is_playing(self):
        return time.monotonic() < self._until

    async def play(self, sc, loop=False, sustain_on_end=False):
        self.plays.append(sc.id)
        self._until = time.monotonic() + self.dur

    async def stop(self):
        self.stops += 1
        self._until = 0.0

    async def settle(self, sid):
        self.settled.append(sid)
        self._until = 0.0
        return True


class FakeCtrl:
    class bus:
        @staticmethod
        def release_manual(): pass

    async def stop_everything(self): pass


class Pin:
    def __init__(self, active=False): self.is_active = active
    def close(self): pass


def _director(tmp_path, *, scenes=None, dur=0.2):
    scenes = scenes or (Sc("fog-bank", "ambient"), Sc("jump", "scare"))
    p = FakePlayer(dur)
    d = ShowDirector(p, FakeStore(*scenes), FakeCtrl(), tmp_path / "show.json")
    d.cooldown_s = 0.0
    return d, p


# ---------- presence hold ----------
def test_no_hold_fires_on_the_first_edge(tmp_path):
    async def go():
        d, _ = _director(tmp_path)
        d.attach_loop(asyncio.get_running_loop())
        d._gpio = Pin(True)
        d._presence_started()
        assert d._trigger.is_set()
    asyncio.run(go())


def test_hold_fires_only_if_they_stay(tmp_path):
    async def go():
        d, _ = _director(tmp_path)
        d.attach_loop(asyncio.get_running_loop())
        d.set_config(presence_hold_s=0.2)
        pin = d._gpio = Pin(True)

        d._presence_started()                     # a quick pass: gone before the hold
        assert d.status()["hold_pending"] and not d._trigger.is_set()
        await asyncio.sleep(0.05)
        pin.is_active = False
        d._cancel_hold()                          # what when_deactivated does
        await asyncio.sleep(0.3)
        assert not d._trigger.is_set() and not d.status()["hold_pending"]

        pin.is_active = True                      # someone walks up and stays
        d._presence_started()
        await asyncio.sleep(0.1)
        assert not d._trigger.is_set()
        await asyncio.sleep(0.2)
        assert d._trigger.is_set()
    asyncio.run(go())


def test_hold_rechecks_the_pin_even_if_an_edge_was_missed(tmp_path):
    async def go():
        d, _ = _director(tmp_path)
        d.attach_loop(asyncio.get_running_loop())
        d.set_config(presence_hold_s=0.1)
        pin = d._gpio = Pin(True)
        d._presence_started()
        pin.is_active = False                     # left, but no deactivate callback arrived
        await asyncio.sleep(0.2)
        assert not d._trigger.is_set()
    asyncio.run(go())


def test_hold_is_clamped_and_validated(tmp_path):
    d, _ = _director(tmp_path)
    d.set_config(presence_hold_s=99); assert d.presence_hold_s == HOLD_MAX_S
    d.set_config(presence_hold_s=-1); assert d.presence_hold_s == 0.0
    d.set_config(presence_hold_s=float("nan")); assert d.presence_hold_s == 0.0
    d.set_config(presence_hold_s=1.25); assert d.presence_hold_s == 1.25


# ---------- sensor on/off ----------
def test_sensor_off_ignores_presence_but_the_button_still_fires(tmp_path):
    async def go():
        d, _ = _director(tmp_path)
        d.attach_loop(asyncio.get_running_loop())
        d._gpio = Pin(True)
        d.set_config(sensor_enabled=False)
        d._presence_started()
        assert not d._trigger.is_set()
        assert d.status()["presence_active"] is True     # still reading, for testing
        d.trigger()                                       # Trigger a scare now
        assert d._trigger.is_set()
    asyncio.run(go())


def test_switching_the_sensor_off_drops_pending_and_latched_triggers(tmp_path):
    async def go():
        d, _ = _director(tmp_path)
        d.attach_loop(asyncio.get_running_loop())
        d.set_config(presence_hold_s=0.15)
        d._gpio = Pin(True)
        d._presence_started()                     # mid-hold
        d.set_config(sensor_enabled=False)
        await asyncio.sleep(0.25)
        assert not d._trigger.is_set()
        d.set_config(sensor_enabled=True, presence_hold_s=0)
        d._presence_started()                     # latched (say, during a cooldown) ...
        d.set_config(sensor_enabled=False)        # ... then switched off: it must not fire later
        assert not d._trigger.is_set()
    asyncio.run(go())


def test_disarm_cancels_a_pending_hold(tmp_path):
    async def go():
        d, _ = _director(tmp_path)
        d.attach_loop(asyncio.get_running_loop())
        d.set_config(presence_hold_s=0.15)
        d._gpio = Pin(True)
        await d.arm(True)
        d._presence_started()
        await d.arm(False)
        assert not d.status()["hold_pending"]
    asyncio.run(go())


# ---------- persistence ----------
def test_options_persist_and_old_configs_keep_todays_behaviour(tmp_path):
    (tmp_path / "show.json").write_text(json.dumps({"cooldown_s": 15, "gpio_pin": 4}))
    d, _ = _director(tmp_path)
    assert (d.ambient_enabled, d.sensor_enabled, d.presence_hold_s) == (True, True, 0.0)
    d.set_config(ambient_enabled=False, sensor_enabled=False, presence_hold_s=1.5)
    saved = json.loads((tmp_path / "show.json").read_text())
    assert (saved["ambient_enabled"], saved["sensor_enabled"], saved["presence_hold_s"]) == (False, False, 1.5)
    assert saved["gpio_pin"] == 4                          # the pin is kept
    d2, _ = _director(tmp_path)
    assert (d2.ambient_enabled, d2.sensor_enabled, d2.presence_hold_s) == (False, False, 1.5)
    assert d2.armed is False                               # arming is never restored


# ---------- sensor-only show loop ----------
def test_sensor_only_show_waits_dark_then_scares_then_goes_dark(tmp_path):
    async def go():
        d, p = _director(tmp_path, dur=0.25)
        d.set_config(ambient_enabled=False)
        await d.arm(True)
        assert p.stops == 1                                # starts from a dark, quiet rig
        await asyncio.sleep(0.3)
        assert d.state == "idle" and p.plays == []         # no ambient, ever
        d.trigger()
        await asyncio.sleep(0.15)
        assert d.state == "scare" and p.plays == ["jump"]
        await asyncio.sleep(1.4)                           # scare ends, loop winds it down
        assert p.settled == ["jump"] and d.state == "idle" and d.current is None
        assert p.plays == ["jump"]
        await d.arm(False)
    asyncio.run(go())


def test_turning_ambient_off_mid_show_winds_the_bed_down(tmp_path):
    async def go():
        d, p = _director(tmp_path, dur=30)
        await d.arm(True)
        await asyncio.sleep(0.2)
        assert d.state == "ambient" and p.plays == ["fog-bank"]
        d.set_config(ambient_enabled=False)
        await asyncio.sleep(0.4)
        assert p.settled == ["fog-bank"] and d.state == "idle"
        d.set_config(ambient_enabled=True)                 # and back on: ambient resumes
        await asyncio.sleep(1.3)
        assert d.state == "ambient" and p.plays[-1] == "fog-bank"
        await d.arm(False)
    asyncio.run(go())


def test_no_ambient_scenes_no_longer_leaves_the_scare_sounding(tmp_path):
    async def go():
        d, p = _director(tmp_path, scenes=(Sc("jump", "scare"),), dur=0.2)
        await d.arm(True)
        d.trigger()
        await asyncio.sleep(1.5)
        assert p.settled == ["jump"] and d.state == "idle"
        await d.arm(False)
    asyncio.run(go())


# ---------- the real player: settle only touches its own scene ----------
class NullTransport:
    name = "null"
    def open(self): pass
    def send(self, f, now): return 1
    def close(self, z=None): pass


class RecAudio:
    def __init__(self): self.calls = []
    def __getattr__(self, n):
        return lambda *a, **k: self.calls.append(n)


@pytest.fixture(scope="module")
def store():
    return Store()


def _short(store, sid, dur):
    sc = store.scenes[sid].model_copy(deep=True)
    sc.duration_s = dur
    return sc


def test_settle_fades_and_goes_dark_after_a_sustained_end(store):
    async def go():
        bus = OutputBus(store, NullTransport())
        p = ShowPlayer(type("C", (), {"store": store})(), bus, audio=RecAudio())
        sc = _short(store, "blood-moon", 0.15)
        await p.play(sc, sustain_on_end=True)
        await asyncio.sleep(0.35)
        assert any(bus.compose(time.monotonic())[0:40])    # final look held
        assert await p.settle(sc.id) is True
        assert not any(bus.compose(time.monotonic()))      # dark
        assert await p.settle(sc.id) is False              # only once
    asyncio.run(go())


def test_settle_leaves_a_scene_played_by_hand_alone(store):
    async def go():
        bus = OutputBus(store, NullTransport())
        p = ShowPlayer(type("C", (), {"store": store})(), bus, audio=None)
        await p.play(_short(store, "blood-moon", 0.1), sustain_on_end=True)
        await asyncio.sleep(0.2)
        await p.play(store.scenes["zombie-horde"])         # user plays something by hand
        assert await p.settle("blood-moon") is False
        assert p.is_playing
        await p.stop()
    asyncio.run(go())


# ---------- API + setup check ----------
def _boot(monkeypatch):
    import haunt_director.api.app as A
    import haunt_director.fpp.provision as P
    monkeypatch.setattr(P, "ensure_all", lambda *a, **k: {"changed": False})

    class T:
        name = "test"
        def open(self): pass
        def send(self, f, now): return 1
        def close(self, zero_frame=None): pass
    monkeypatch.setattr(A, "make_transport", lambda s: T())
    return A.app


def test_show_config_api_round_trip(monkeypatch):
    app = _boot(monkeypatch)
    with TestClient(app) as c:
        before = c.get("/api/show").json()
        try:
            assert c.post("/api/show/config", json={"presence_hold_s": 6}).status_code == 422
            assert c.post("/api/show/config", json={"presence_hold_s": -0.5}).status_code == 422
            r = c.post("/api/show/config", json={"presence_hold_s": 1.5, "sensor_enabled": False,
                                                  "ambient_enabled": False}).json()
            assert (r["presence_hold_s"], r["sensor_enabled"], r["ambient_enabled"]) == (1.5, False, False)
            assert r["gpio_pin"] == before["gpio_pin"]                   # pin untouched
            assert c.get("/api/play/state").json()["presence"]["enabled"] is False

            c.post("/api/show/arm", json={"armed": True})                # sensor-only: stays dark
            time.sleep(0.4)
            assert c.get("/api/show").json()["state"] == "idle"
            assert c.get("/api/play/status").json()["playing"] is False
            c.post("/api/show/trigger")                                  # the button still fires
            time.sleep(0.4)
            st = c.get("/api/show").json()
            assert st["state"] == "scare" and st["current"] in st["scares"]
            c.post("/api/show/arm", json={"armed": False})
        finally:
            c.post("/api/show/arm", json={"armed": False})
            c.post("/api/show/config", json={k: before.get(k, d) for k, d in
                                             (("presence_hold_s", 0.0), ("sensor_enabled", True),
                                              ("ambient_enabled", True))})


def test_setup_check_flags_a_switched_off_sensor(monkeypatch):
    from haunt_director import system as S
    from haunt_director.config import Settings
    monkeypatch.setattr(S, "_http", lambda url, timeout=3.0: (0, None))
    monkeypatch.setattr(S, "_run", lambda cmd, timeout=8.0, input_text=None: (127, ""))
    monkeypatch.setattr(S, "_read", lambda p: None)
    ctx = {"settings": Settings(), "vitals": {}, "bus_stats": {}, "audio_state": {},
           "show_status": {"gpio_pin": 4, "gpio_ok": True, "presence_active": False, "sensor_enabled": False},
           "fpp": S.fpp_status()}
    c = {x["id"]: x for x in S.run_checks(ctx)}["sensor"]
    assert c["status"] == "warn" and "switched off" in c["detail"]
    ctx["show_status"]["sensor_enabled"] = True
    assert {x["id"]: x for x in S.run_checks(ctx)}["sensor"]["status"] == "ok"


# ---------- the sensor input fails safe ----------
@pytest.mark.parametrize("active_high", [True, False])
def test_a_loose_sensor_reads_as_nobody_there(tmp_path, monkeypatch, active_high):
    """The input is pulled toward 'clear' (down for a HIGH-on-presence sensor, up
    for a LOW one), so an unplugged or broken cable can't float into phantom
    scares. gpiozero derives the active level from the pull, which keeps
    'active' meaning 'present' either way."""
    import sys
    import types
    import haunt_director.show as S
    made = {}

    class FakeDID:
        def __init__(self, pin, **kw):
            made.update(kw, pin=pin)
            self.when_activated = self.when_deactivated = None
        def close(self): pass
    monkeypatch.setitem(sys.modules, "gpiozero", types.SimpleNamespace(DigitalInputDevice=FakeDID))
    monkeypatch.setattr(S, "_ensure_pin_factory", lambda: None)
    d, _ = _director(tmp_path)
    d.gpio_pin, d.gpio_active_high = 4, active_high
    d.start_gpio()
    assert made["pin"] == 4 and made["pull_up"] is (not active_high)
    assert "active_state" not in made                  # implied by the pull; gpiozero refuses both
    assert d._gpio is not None and d._gpio.when_activated == d._on_presence
