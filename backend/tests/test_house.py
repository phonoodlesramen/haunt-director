"""House lights: lights only, slow colour drift, their own schedule, and the
show (or any playing scene) always takes priority."""
import asyncio
import json
import random
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import haunt_director.house as H
from haunt_director.output import OutputBus
from haunt_director.store import Store

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


# ---------------- drift ----------------
def test_one_color_holds_steady_and_two_glide_and_loop():
    assert H.drift_rgb(["#ff8000"], 12345.0, 8) == (255, 128, 0)
    cols, step = ["#000000", "#ffffff"], 8 * 60
    assert H.drift_rgb(cols, 0, 8) == (0, 0, 0)
    assert H.drift_rgb(cols, step * 0.39, 8) == (0, 0, 0)            # still holding
    mid = H.drift_rgb(cols, step * 0.7, 8)
    assert mid[0] in (127, 128) and mid[0] == mid[1] == mid[2]       # halfway through the glide
    assert H.drift_rgb(cols, step * 0.999, 8)[0] > 250                # nearly there
    assert H.drift_rgb(cols, step * 1.2, 8) == (255, 255, 255)        # holding the second
    assert H.drift_rgb(cols, step * 2.0, 8) == (0, 0, 0)              # and round again
    prev = None
    for k in range(0, 2 * step, 5):                                   # never jumps
        c = H.drift_rgb(cols, k, 8)
        if prev is not None:
            assert abs(c[0] - prev[0]) <= 8          # at most ~1.4 levels a second
        prev = c


def test_lights_are_out_of_step():
    phases = [H.phase_for(i) for i in range(3)]
    assert len({round(p, 3) for p in phases}) == 3


RUNNER = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {window: {}, document: {addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; },
             getElementById(){ return null; }}, setInterval(){ return 0; }, setTimeout(){ return 0; }, clearTimeout(){},
             performance: {now: () => 0}, console, Math, Date, JSON};
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length - 1], "utf8"), ctx);
const job = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(job.map(c => ctx.houseDrift(c.colors, c.t, c.hold, ctx.housePhase(c.i)))));
"""


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_card_previews_exactly_what_the_yard_shows():
    rng = random.Random(3)
    cases = []
    for _ in range(3000):
        n = rng.randint(1, 4)
        cases.append({"colors": ["#%06x" % rng.randint(0, 0xffffff) for _ in range(n)],
                      "t": rng.choice([rng.uniform(0, 1e9), rng.uniform(0, 7200), 1791232073.25]),
                      "hold": rng.choice([1, 2.5, 8, 30, 60]), "i": rng.randint(0, 5)})
    js = subprocess.run([NODE, "-e", RUNNER, str(ROOT / "web" / "house.js")], input=json.dumps(cases),
                        capture_output=True, text=True, timeout=60, check=True)
    got = json.loads(js.stdout)
    bad = [(c, g) for c, g in zip(cases, got)
           if tuple(g) != H.drift_rgb(c["colors"], c["t"], c["hold"], H.phase_for(c["i"]))]
    assert not bad, bad[:3]


# ---------------- schedule ----------------
def _sunset(day):
    return datetime(day.year, day.month, day.day, 18, 30).astimezone()


def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm).astimezone()


def test_dusk_to_a_set_time_and_past_midnight():
    w = {"enabled": True, "start_mode": "dusk", "dusk_offset_min": -15, "end": "23:00", "days": list(range(7))}
    s, e = H.window_bounds(w, date(2026, 10, 14), _sunset)
    assert (s.hour, s.minute, e.hour, e.minute, e.day) == (18, 15, 23, 0, 14)
    late = dict(w, end="01:30")
    s, e = H.window_bounds(late, date(2026, 10, 14), _sunset)
    assert e.day == 15 and (e.hour, e.minute) == (1, 30)
    assert H.active_window([late], _at(2026, 10, 15, 0, 45), _sunset)            # yesterday's window, still on
    assert not H.active_window([late], _at(2026, 10, 15, 1, 31), _sunset)
    assert not H.active_window([w], _at(2026, 10, 14, 18, 0), _sunset)          # before dusk - 15
    assert H.active_window([w], _at(2026, 10, 14, 18, 20), _sunset)


def test_days_of_the_week_and_clock_starts():
    w = {"enabled": True, "start_mode": "clock", "time": "19:00", "end": "22:00", "days": [4, 5]}   # Fri, Sat
    fri, wed = date(2026, 10, 16), date(2026, 10, 14)
    assert fri.weekday() == 4 and wed.weekday() == 2
    assert H.window_bounds(w, wed, _sunset) is None
    s, e = H.window_bounds(w, fri, _sunset)
    assert (s.hour, e.hour) == (19, 22)
    assert H.next_start([w], _at(2026, 10, 14, 12, 0), _sunset) == s
    assert not H.active_window([dict(w, enabled=False)], _at(2026, 10, 16, 20, 0), _sunset)


def test_config_is_cleaned():
    c = H.clean_config({"mode": "party", "brightness": 7, "hold_min": 0,
                        "lights": {"a": {"colors": ["#ABCDEF", "nope", "#000000", "#111111", "#222222", "#333333"]},
                                   "zz": {"colors": ["#ffffff"]}},
                        "windows": [{"label": "x" * 99, "start_mode": "clock", "time": "25:99", "end": "7:5",
                                     "days": [9, 1, 1], "dusk_offset_min": 999}]}, ["a", "b"])
    assert c["mode"] == "off" and c["brightness"] == 1.0 and c["hold_min"] == H.HOLD_MIN
    assert c["lights"]["a"]["colors"] == ["#abcdef", "#000000", "#111111", "#222222"]
    assert c["lights"]["b"]["colors"] == H.DEFAULT_COLORS and "zz" not in c["lights"]
    w = c["windows"][0]
    assert len(w["label"]) == 40 and w["time"] == "18:00" and w["end"] == "07:05" and w["days"] == [1]
    assert w["dusk_offset_min"] == 180


# ---------------- the controller ----------------
class FakeBus:
    def __init__(self): self.house, self.cleared = None, 0
    def set_house(self, src): self.house = src
    def clear_held_if_idle(self): self.cleared += 1


class FakeDirector:
    armed = False
    def _sunset_dt(self, day): return _sunset(day)


class FakePlayer:
    is_playing = False


def _house(tmp_path):
    store = Store()
    h = H.HouseLights(store, FakeBus(), FakePlayer(), FakeDirector(), tmp_path / "house.json")
    return h, store


def test_house_lights_are_lights_only_and_yield_to_the_show(tmp_path):
    h, store = _house(tmp_path)
    assert h.cfg["mode"] == "off" and len(h.cfg["lights"]) == 3        # the three fixtures, not the fogger
    h.tick()
    assert h.bus.house is None and h.reason == "off"
    h.set_mode("on")
    assert h.bus.house is not None and h.active and h.bus.cleared == 1
    chans = h.bus.house(time.monotonic() + 10)                         # fully faded in
    fog = store.devices["fog_main"].dmx_address
    assert chans and all(c < fog for c in chans)                       # never the fogger's channels
    h.director.armed = True
    h.tick()
    assert h.bus.house is None and h.reason == "show"                 # the show takes the yard at once
    h.director.armed = False
    h.player.is_playing = True
    h.tick()
    assert h.bus.house is None and h.reason == "playing"
    h.player.is_playing = False
    h.tick()
    assert h.bus.house is not None and h._level(time.monotonic()) < 0.2   # back, fading in from dark


def test_stop_everything_keeps_them_off_until_switched_on_again(tmp_path):
    h, _ = _house(tmp_path)
    h.set_mode("on")
    h.snooze()
    h.tick()
    assert h.bus.house is None and h.reason == "stopped"
    h.set_mode("on")                                                   # pressing On again lifts it
    assert h.bus.house is not None


def test_schedule_mode_follows_the_windows_and_fades_out(tmp_path, monkeypatch):
    h, _ = _house(tmp_path)
    now = datetime.now().astimezone()
    start = (now - timedelta(minutes=5)).strftime("%H:%M")
    end = (now + timedelta(minutes=30)).strftime("%H:%M")
    h.set_config({"mode": "schedule", "windows": [{"label": "Tonight", "start_mode": "clock", "time": start,
                                                   "end": end, "days": list(range(7))}]})
    assert h.active and h.reason == "schedule"
    st = h.status()["status"]
    assert st["window"] == "Tonight" and st["until"] == end
    h.set_config({"windows": []})                                       # window gone: fade out, then off
    assert h.active and h.reason == "waiting" and h._off_at is not None
    monkeypatch.setattr(H, "FADE_S", 0.0)
    h._off_at = time.monotonic() - 1
    h.tick()
    assert not h.active and h.bus.house is None
    assert json.loads((tmp_path / "house.json").read_text())["mode"] == "schedule"


# ---------------- the bus layer ----------------
def test_the_bus_shows_house_lights_only_when_nothing_else_is_on():
    store = Store()

    class T:
        def open(self): pass
        def send(self, f, now): return 1
        def close(self, zero_frame=None): pass
    bus = OutputBus(store, T(), nch=64)
    bus.set_house(lambda now: {1: 200, 49: 255})            # 49 is the fogger: the guard must still apply
    bus.fog_enabled = False
    f = bus.compose(time.monotonic())
    assert f[0] == 200 and f[48] == 0
    bus.set_scene(lambda now: {1: 7})
    assert bus.compose(time.monotonic())[0] == 7             # a scene replaces house lights outright
    bus.end_scene(hold=True)
    assert bus.compose(time.monotonic())[0] == 7             # so does a held last look...
    bus.clear_held_if_idle()
    assert bus.compose(time.monotonic())[0] == 200           # ...until house lights clear it
    bus.set_manual("x", {1: 99})
    assert bus.compose(time.monotonic())[0] == 99            # Live cards still sit on top
    bus.set_house(None)
    bus.release_manual()
    assert bus.compose(time.monotonic())[0] == 0


# ---------------- API ----------------
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
    return A


def test_the_api_saves_switches_and_stop_everything_snoozes(monkeypatch, tmp_path):
    A = _boot(monkeypatch)
    with TestClient(A.app) as c:
        h = A.app.state.house
        h.path = tmp_path / "house.json"                      # don't touch the real data folder
        h.cfg = H.default_config(h._light_ids())
        r = c.get("/api/house").json()
        assert r["mode"] == "off" and set(r["lights"]) == {"light_skelly", "light_path", "light_porch"}
        assert r["light_order"][0] == "light_skelly" and "status" in r
        assert c.post("/api/house/mode", json={"mode": "disco"}).status_code == 400
        r = c.put("/api/house", json={"brightness": 0.5, "lights": {"light_path": {"on": True, "colors": ["#00ff00"]}}}).json()
        assert r["brightness"] == 0.5 and r["lights"]["light_path"]["colors"] == ["#00ff00"]
        r = c.post("/api/house/mode", json={"mode": "on"}).json()
        assert r["status"]["active"] is True
        st = c.get("/api/play/state").json()
        assert st.get("house") is True and st["devices"]["light_path"]["hex"] == "#00ff00"
        assert c.post("/api/stop").status_code == 200
        r = c.get("/api/house").json()
        assert r["status"]["active"] is False and r["status"]["reason"] == "stopped"
        assert c.get("/api/play/state").json().get("house") is None
        c.post("/api/house/mode", json={"mode": "off"})


def test_the_live_tab_has_the_house_lights_card():
    html = (ROOT / "web" / "index.html").read_text()
    assert 'id="houseCard"' in html and html.index('id="houseCard"') < html.index('<div id="lights">')
    assert html.index('src="app.js') < html.index('src="house.js')
    for m in ("off", "schedule", "on"):
        assert f"houseMode('{m}')" in html
