"""Sim only: the browser's evaluator (web/sim.js) must light the stage exactly
the way the rig would, and nothing that plays in the simulator may reach the
yard while the show is armed."""
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from haunt_director.domain.scenes import Scene, Event, evaluate, SIZABLE

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
TYPES = ["set_color", "set_brightness", "blackout", "fade_color", "fade_brightness", "pulse", "strobe", "fog"]

RUNNER = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {window: {}, document: {addEventListener(){}, querySelectorAll(){ return []; }},
             performance: {now: () => 0}, requestAnimationFrame(){ return 0; }, cancelAnimationFrame(){},
             localStorage: {getItem(){ return null; }, setItem(){}}, console};
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length - 1], "utf8"), ctx);
const job = JSON.parse(fs.readFileSync(0, "utf8"));
const out = job.cases.map(c => c.times.map(t => ctx.simEvaluate(c.scene, t, job.cap)));
process.stdout.write(JSON.stringify(out));
"""


def _py_state(sc, t, cap):
    out = {}
    for dev, s in evaluate(sc, t).items():
        hz = min(s.strobe_hz, cap) if (s.strobe_hz and cap) else s.strobe_hz
        out[dev] = {"hex": "#%02x%02x%02x" % s.rgb, "brightness": s.brightness, "strobe": s.strobe_hz > 0,
                    "strobe_hz": round(hz, 2), "fog": s.fog}
    return out


def _random_scene(rng, i):
    tracks = {}
    for dev in ("a", "b"):
        evs = []
        for k in range(rng.randint(1, 30)):
            typ = rng.choice(TYPES)
            t = rng.choice([round(rng.uniform(0, 20), rng.choice([1, 2])), 5.0, 10.0])   # some share a time
            evs.append({"id": f"{dev}{k}", "t": t, "type": typ,
                        "duration_s": round(rng.uniform(0.1, 4), 2) if typ in SIZABLE else 0.0,
                        "params": {"color": "#%06x" % rng.randint(0, 0xffffff), "brightness": round(rng.random(), 3),
                                   "rate_hz": round(rng.uniform(1, 12), 2), "intensity": round(rng.random(), 3),
                                   "curve": rng.choice(["linear", "ease_in", "ease_out", "ease_in_out"])}})
        tracks[dev] = evs
    return {"id": f"r{i}", "name": "r", "duration_s": 22.0, "tracks": tracks}


@pytest.mark.skipif(not NODE, reason="needs node")
@pytest.mark.parametrize("cap", [0, 4.0])
def test_the_simulator_lights_the_stage_exactly_like_the_rig(cap):
    rng = random.Random(7)
    cases = []
    for i in range(120):                                       # made-up scenes, every event type
        sc = _random_scene(rng, i)
        times = sorted({round(rng.uniform(-0.5, 23), 3) for _ in range(40)}
                       | {e["t"] for evs in sc["tracks"].values() for e in evs})
        cases.append({"scene": sc, "times": times})
    for f in sorted((ROOT / "data" / "scenes").glob("*.json"))[:40]:     # and the real ones
        sc = json.loads(f.read_text())
        d = float(sc.get("duration_s") or 10)
        cases.append({"scene": sc, "times": [round(d * k / 97, 3) for k in range(98)]})
    js = subprocess.run([NODE, "-e", RUNNER, str(ROOT / "web" / "sim.js")], input=json.dumps({"cases": cases, "cap": cap}),
                        capture_output=True, text=True, timeout=120, check=True)
    got = json.loads(js.stdout)
    checked = bad = 0
    for c, frames in zip(cases, got):
        sc = Scene.model_validate(c["scene"])
        for t, fr in zip(c["times"], frames):
            want = _py_state(sc, t, cap)
            assert set(fr) == set(want)
            for dev, w in want.items():
                g = fr[dev]
                checked += 1
                same = (g["hex"], g["strobe"], g["fog"]) == (w["hex"], w["strobe"], w["fog"]) \
                    and abs(g["brightness"] - w["brightness"]) <= 0 and abs(g["strobe_hz"] - w["strobe_hz"]) < 0.011
                if not same:
                    bad += 1
                    if bad < 5:
                        print("mismatch", c["scene"]["id"], dev, t, g, w)
    assert checked > 10000 and bad == 0


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


def test_while_the_show_is_armed_play_seek_and_mute_cant_reach_the_yard(monkeypatch):
    A = _boot(monkeypatch)
    with TestClient(A.app) as c:
        sid = sorted(s["id"] for s in c.get("/api/scenes").json())[0]
        assert "strobe_max_hz" in c.get("/api/output").json()
        A.app.state.show.armed = True
        try:
            for path, body in ((f"/api/scenes/{sid}/play", {}), ("/api/play/seek", {"t": 3}),
                               ("/api/play/mute", {"mute": ["music"]})):
                r = c.post(path, json=body)
                assert r.status_code == 409 and "simulator" in r.json()["detail"], path
            assert not A.app.state.player.is_playing
        finally:
            A.app.state.show.armed = False
        assert c.post(f"/api/scenes/{sid}/play", json={}).status_code == 200      # disarmed: live as before
        c.post("/api/play/stop")


def test_the_page_has_the_sim_live_switch():
    html = (ROOT / "web" / "index.html").read_text()
    assert html.count('data-pm role="group"') == 2                            # Scenes tab + editor
    assert html.index('src="sim.js') < html.index('src="fullview.js') < html.index('src="app.js')
    assert "fsOpenCurrent()" in html and "fsOpen(__editScene())" in html                # stage + editor ⛶
    assert "fsOpen('${s.id}',{autoplay:true})" in (ROOT / "web" / "app.js").read_text()  # every scene card
    js = (ROOT / "web" / "sim.js").read_text()
    assert "window.__armed ? \"sim\"" in js                                    # armed: always the simulator


CLOCK_RUNNER = r"""
const fs = require("fs"), vm = require("vm");
let NOW = 0, RAF = [];
const ctx = {window: {}, document: {addEventListener(){}, querySelectorAll(){ return []; }, getElementById(){ return null; }},
             performance: {now: () => NOW}, requestAnimationFrame(f){ RAF.push(f); return RAF.length; }, cancelAnimationFrame(){},
             localStorage: {getItem(){ return null; }, setItem(){}}, console, Math, isFinite};
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length - 1], "utf8"), ctx);
// a stand-in for the browser's <audio>: the test decides when it starts, how fast it runs, when it stalls
vm.runInContext(`
  var AUDIO = {music: null, scene: null};
  var EL = null;
  function playAudio(s, loop, mute, from){ stopAudio(); if(!s.audio || !s.audio.music) return;
    EL = {currentTime: 0, paused: true, readyState: 0, duration: s.__clip, error: null, __from: from || 0};
    AUDIO.music = EL; AUDIO.scene = s.id; }
  function stopAudio(){ AUDIO.music = null; AUDIO.scene = null; EL = null; }
`, ctx);
const job = JSON.parse(fs.readFileSync(0, "utf8"));
const out = [];
function frame(){ const f = RAF; RAF = []; f.forEach(cb => cb(NOW)); }
for(const c of job.cases){
  NOW = 1000; RAF = [];
  const sc = {id: "s", name: "s", duration_s: c.dur, tracks: {}, audio: c.music ? {music: "m.mp3"} : null, __clip: c.clip};
  vm.runInContext("simStop()", ctx);
  ctx.simPlay(sc, {from: c.from || 0});
  const rows = [];
  let ct = null, startAt = c.sound_starts_ms, rate = c.rate || 1;
  for(let ms = 0; ms <= c.run_ms; ms += 16){
    NOW = 1000 + ms;
    const el = vm.runInContext("EL", ctx);
    if(c.seek_at_ms !== undefined && ms >= c.seek_at_ms && !c.__seeked){ c.__seeked = true; ctx.simSeek(c.seek_to); startAt = ms + c.sound_starts_ms; ct = null; }
    const e2 = vm.runInContext("EL", ctx);
    if(e2 && startAt !== null && ms >= startAt && !(c.blocked)){
      const stalled = c.stall && ms >= c.stall[0] && ms < c.stall[1];
      if(ct === null) ct = e2.__from % (e2.duration || 1e9);
      else if(!stalled) ct += rate * 0.016;
      e2.paused = false; e2.readyState = stalled ? 2 : 4;
      e2.currentTime = Math.round(ct * 1000) / 1000;
    }
    frame();
    rows.push([ms, ctx.simTime(), e2 ? e2.currentTime : null, ctx.__simClock()]);
  }
  out.push(rows);
}
process.stdout.write(JSON.stringify(out));
"""


def _clock(cases):
    js = subprocess.run([NODE, "-e", CLOCK_RUNNER, str(ROOT / "web" / "sim.js")], input=json.dumps({"cases": cases}),
                        capture_output=True, text=True, timeout=120, check=True)
    return json.loads(js.stdout)


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_sim_lights_wait_for_the_sound_then_follow_it():
    late, slow, stall, blocked, bed, seek, silent = _clock([
        {"dur": 60, "clip": 60.5, "music": True, "sound_starts_ms": 400, "run_ms": 3000},          # sound loads late
        {"dur": 60, "clip": 60.5, "music": True, "sound_starts_ms": 100, "run_ms": 20000, "rate": 0.99},  # its clock runs 1% slow
        {"dur": 60, "clip": 60.5, "music": True, "sound_starts_ms": 100, "run_ms": 6000, "stall": [2000, 3000]},
        {"dur": 60, "clip": 60.5, "music": True, "sound_starts_ms": 0, "run_ms": 4000, "blocked": True},  # the browser won't play it
        {"dur": 90, "clip": 30, "music": True, "sound_starts_ms": 100, "run_ms": 2000},            # a short score looping under it
        {"dur": 60, "clip": 60.5, "music": True, "sound_starts_ms": 300, "run_ms": 3000, "seek_at_ms": 1000, "seek_to": 40},
        {"dur": 60, "clip": None, "music": False, "sound_starts_ms": None, "run_ms": 1000},
    ])
    # held at the start until the sound moves, then in step with it
    assert all(t == 0 and k == "wait" for ms, t, ct, k in late if ms < 400)
    assert late[-1][3] == "music"
    assert max(abs(t - ct) for ms, t, ct, k in late if ms > 500) < 0.02
    # a slow sound clock: the lights stay with the sound, not the wall clock
    assert max(abs(t - ct) for ms, t, ct, k in slow if ms > 1000) < 0.02
    assert abs(slow[-1][1] - 19.9 * 0.99) < 0.05
    # a stall: back in step within a frame or two of the sound moving again
    assert max(abs(t - ct) for ms, t, ct, k in stall if ms > 3100) < 0.02
    # sound that never starts: the lights go on without it after a short wait
    assert blocked[-1][3] == "clock" and 1.0 < blocked[-1][1] < 1.6
    # a short looping bed isn't a clock for the scene
    assert bed[-1][3] == "clock" and abs(bed[-1][1] - 1.9) < 0.05
    # a seek holds at the new spot until the sound gets there, then follows it
    assert all(abs(t - 40) < 1e-9 and k == "wait" for ms, t, ct, k in seek if 1008 <= ms < 1300)
    assert seek[-1][3] == "music" and max(abs(t - ct) for ms, t, ct, k in seek if ms > 1400) < 0.02
    # no music: this device's clock, from the first frame
    assert silent[0][3] == "clock" and abs(silent[-1][1] - 1.0) < 0.02


def test_the_stage_shows_exact_states_without_easing():
    stage = (ROOT / "web" / "stage.js").read_text()
    assert "const k = STATE.exact ? 1 : 0.20;" in stage
    assert "exact: true" in (ROOT / "web" / "sim.js").read_text()
    assert "exact: true" in (ROOT / "web" / "fullview.js").read_text()
    html = (ROOT / "web" / "index.html").read_text()
    assert "the simulator keeps its lights with this device's own sound" in html
