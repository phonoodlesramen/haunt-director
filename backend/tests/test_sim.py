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
