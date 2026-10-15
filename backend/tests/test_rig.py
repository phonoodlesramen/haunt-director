"""The rig editor: more (and different) fixtures than the reference rig, added
from the app. Channel block sizing, rig checks, followers (a new light joins
the scenes that already exist), the profile library, and the API."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from haunt_director.domain.devices import Device
from haunt_director.domain.profiles import FixtureProfile
from haunt_director.domain.scenes import Scene, evaluate
from haunt_director.domain import rig

LIGHT = FixtureProfile.model_validate({
    "id": "l8", "vendor": "T", "model": "PAR", "mode_name": "8CH", "channel_count": 8, "has_master_dimmer": True,
    "channels": [{"offset": 0, "role": "dimmer"}, {"offset": 1, "role": "red"}, {"offset": 2, "role": "green"},
                 {"offset": 3, "role": "blue"}, {"offset": 4, "role": "white"},
                 {"offset": 5, "role": "strobe", "off_band": {"lo": 0, "hi": 0}},
                 {"offset": 6, "role": "function"}, {"offset": 7, "role": "speed"}]})
FOG = FixtureProfile.model_validate({
    "id": "f1", "vendor": "T", "model": "Fog", "mode_name": "1CH", "channel_count": 1,
    "channels": [{"offset": 0, "role": "fog", "off_band": {"lo": 0, "hi": 5}}]})
PROFS = {"l8": LIGHT, "f1": FOG}


def light(i, addr, **kw):
    return Device(id=i, name=kw.pop("name", i.title()), kind="dmx_fixture", profile_id=kw.pop("profile_id", "l8"),
                  dmx_address=addr, **kw)


def fogger(i, addr, **kw):
    return Device(id=i, name=i.title(), kind="dmx_fog", profile_id="f1", dmx_address=addr, **kw)


REF = [light("a", 1), light("b", 17), light("c", 33), fogger("fog", 49)]


# ---------------------------------------------------------------- channel block
def test_the_block_keeps_its_configured_size_and_grows_to_reach_every_device():
    assert rig.needed_end(REF, PROFS) == 49
    assert rig.block_end(REF, PROFS, 64) == 64                     # the reference rig: unchanged
    more = REF + [light("d", 65), light("e", 81)]
    assert rig.needed_end(more, PROFS) == 88
    assert rig.block_end(more, PROFS, 64) == 88                    # grows past 64 on its own
    assert rig.block_end([light("z", 509)], PROFS, 64) == 512      # never past one universe
    assert rig.block_end([], PROFS, 64) == 64


def test_the_output_bus_grows_to_cover_a_new_device_and_never_shrinks_while_running():
    from haunt_director.output import OutputBus

    class S:
        devices = {d.id: d for d in REF}
        profiles = PROFS
        def profile_for(self, d): return PROFS.get(d.profile_id)

    class T:
        name = "t"
        def open(self): pass
        def send(self, f, now): return 1
        def close(self, zero_frame=None): pass
    bus = OutputBus(S(), T(), nch=64)
    bus.set_manual("d", {70: 200})
    assert len(bus.compose(time.monotonic())) == 64                # channel 70 doesn't fit yet
    bus.ensure_channels(88)
    f = bus.compose(time.monotonic())
    assert len(f) == 88 and f[69] == 200
    bus.ensure_channels(64)
    assert bus.nch == 88


# ---------------------------------------------------------------- checks
def test_checks_catch_overlaps_wrong_profiles_and_bad_addresses():
    bad = REF + [light("over", 20), light("nop", 97, profile_id="missing"),
                 Device(id="fl", name="Fog Light", kind="dmx_fixture", profile_id="f1", dmx_address=120),
                 fogger("lf", 130).model_copy(update={"profile_id": "l8"}), light("far", 509),
                 Device(id="noaddr", name="No Addr", kind="dmx_fixture", profile_id="l8")]
    msgs = {i["device"]: i for i in rig.issues(bad, PROFS)}
    assert msgs["over"]["level"] == "error" and "overlaps" in msgs["over"]["message"]
    assert "isn't installed" in msgs["nop"]["message"]
    assert "isn't a light profile" in msgs["fl"]["message"]
    assert "no fog channel" in msgs["lf"]["message"]
    assert "outside one DMX universe" in msgs["far"]["message"]
    assert "no DMX address" in msgs["noaddr"]["message"]
    assert rig.issues(REF, PROFS) == []


def test_follow_problems_are_warnings_not_errors():
    devs = REF + [light("d", 65, follows="gone"), light("e", 81, follows="fog"), light("f", 97, follows="f"),
                  light("g", 113, follows="h"), light("h", 129, follows="g")]
    iss = rig.issues(devs, PROFS)
    assert iss and all(i["level"] == "warn" for i in iss)
    words = " ".join(i["message"] for i in iss)
    for w in ("no longer exists", "lights can only follow lights", "follow itself", "loop"):
        assert w in words


# ---------------------------------------------------------------- followers
def _scene(tracks):
    return Scene.model_validate({"id": "s", "name": "s", "duration_s": 10, "tracks": tracks})


RED = [{"id": "1", "t": 0, "type": "set_color", "params": {"color": "#ff0000", "brightness": 1.0}}]
BLUE = [{"id": "2", "t": 0, "type": "set_color", "params": {"color": "#0000ff", "brightness": 1.0}}]


def test_a_follower_plays_its_leaders_part_unless_it_has_its_own():
    fol = rig.follow_map(REF + [light("d", 65, follows="a"), light("e", 81, follows="d"),
                                light("off", 97, follows="a", enabled=False), light("x", 113, follows="fog")])
    assert fol == {"d": "a", "e": "d"}                              # disabled and wrong-kind followers ignored
    st = evaluate(_scene({"a": RED, "b": BLUE}), 1.0, fol)
    assert st["d"].rgb == (255, 0, 0) and st["e"].rgb == (255, 0, 0)   # through the chain
    st = evaluate(_scene({"a": RED, "d": BLUE}), 1.0, fol)
    assert st["d"].rgb == (0, 0, 255) and st["e"].rgb == (0, 0, 255)   # its own part wins, and passes down
    st = evaluate(_scene({"a": RED, "d": []}), 1.0, fol)
    assert st["d"].rgb == (255, 0, 0)                               # an emptied track borrows again
    assert "d" not in evaluate(_scene({"b": BLUE}), 1.0, fol)       # nothing to borrow: plays nothing
    assert rig.part_source("g", {"h": RED}, {"g": "h", "h": "g"}) == "h"
    assert rig.part_source("g", {}, {"g": "h", "h": "g"}) is None   # loops end


def test_a_follower_lights_up_on_the_rig_at_its_own_address():
    from haunt_director.player import scene_channels

    class S:
        devices = {d.id: d for d in REF + [light("d", 65, follows="a")]}
        follows = rig.follow_map(devices.values())
        def device(self, i): return self.devices.get(i)
        def profile_for(self, d): return PROFS.get(d.profile_id)
    ch = scene_channels(S(), _scene({"a": RED}), 1.0)
    assert ch[1] == 255 and ch[2] == 255                            # leader: dimmer, red
    assert ch[65] == 255 and ch[66] == 255 and ch[68] == 0          # follower: same look, its own channels
    assert scene_channels(S(), _scene({"a": RED}), 1.0, mute={"d"})[65] == 0


# ---------------------------------------------------------------- adding devices
def test_new_devices_get_the_next_free_slot_a_unique_id_and_a_spot_on_the_stage():
    assert rig.suggest_address(REF, PROFS, 8) == 65                 # after the fogger's slot at 49
    assert rig.suggest_address([light("a", 1), light("c", 33)], PROFS, 8) == 17   # fills a gap
    assert rig.suggest_address(REF, PROFS, 8, exclude="b") == 17    # moving a light: its own slot counts as free
    full = [light(f"l{i}", 1 + 8 * i) for i in range(64)]
    assert rig.suggest_address(full, PROFS, 8) is None
    assert rig.new_id("Porch Light", ["porch_light"]) == "porch_light_2"
    assert rig.new_id("   ", []) == "device"
    placed = [light("a", 1).model_copy(update={"sim_position": {"x": 0.1, "y": .6}}),
              light("b", 17).model_copy(update={"sim_position": {"x": 0.2, "y": .6}})]
    p = rig.default_placement(placed, "dmx_fixture")
    assert p["sim_position"]["x"] > 0.5 and p["aim"]["x"] == p["sim_position"]["x"]
    assert "aim" not in rig.default_placement(placed, "dmx_fog")


# ---------------------------------------------------------------- profile library
def test_the_profile_library_ships_starters_and_your_own_profiles_win(tmp_path):
    from haunt_director.store import Store, EXAMPLE_DIR
    d = tmp_path / "data"
    (d / "profiles").mkdir(parents=True)
    (d / "devices.json").write_text(json.dumps([light("a", 1, profile_id="generic-rgbw-4ch").model_dump()]))
    old = json.loads((EXAMPLE_DIR / "profiles" / "holdlamp-rgbw-8ch.json").read_text())
    old.pop("tested"); old.pop("notes")                             # an old install's copy, from before the library
    (d / "profiles" / "holdlamp-rgbw-8ch.json").write_text(json.dumps(old))
    mine = json.loads((EXAMPLE_DIR / "profiles" / "generic-rgbw-4ch.json").read_text())
    mine["model"] = "My RGBW can"
    (d / "profiles" / "generic-rgbw-4ch.json").write_text(json.dumps(mine))
    st = Store(d)
    assert {"generic-rgb-3ch", "generic-rgbw-4ch", "generic-dimmer-rgbw-strobe-6ch",
            "holdlamp-rgbw-8ch", "generic-fog-1ch"} <= set(st.profiles)
    assert st.profile_source["holdlamp-rgbw-8ch"] == "library" and st.profiles["holdlamp-rgbw-8ch"].tested
    assert st.profile_source["generic-rgbw-4ch"] == "yours" and st.profiles["generic-rgbw-4ch"].model == "My RGBW can"
    for pid in ("generic-rgb-3ch", "generic-rgbw-4ch", "generic-dimmer-rgbw-strobe-6ch"):
        p = st.profiles[pid]
        assert not p.tested and "manual" in p.notes and rig.profile_kind(p) == "light"
        assert p.channel_count == len(p.channels)


def test_starter_profiles_resolve_to_sensible_channels():
    from haunt_director.domain.resolve import resolve_fixture
    from haunt_director.store import Store
    st = Store()
    v = resolve_fixture(st.profiles["generic-rgbw-4ch"], 10, rgb=(255, 128, 0), brightness=128)
    assert v == {10: 128, 11: 64, 12: 0, 13: 0}                     # no dimmer: brightness mixed into color
    v = resolve_fixture(st.profiles["generic-dimmer-rgbw-strobe-6ch"], 1, rgb=(255, 0, 0), brightness=200)
    assert v[1] == 200 and v[2] == 255 and v[6] <= 5                # dimmer, red, strobe off
    v = resolve_fixture(st.profiles["generic-dimmer-rgbw-strobe-6ch"], 1, rgb=(255, 0, 0), strobe_hz=4)
    assert 6 <= v[6] <= 255


# ---------------------------------------------------------------- the API
@pytest.fixture
def app_in(tmp_path, monkeypatch):
    """The real app on a throwaway copy of the example data."""
    import haunt_director.api.app as A
    import haunt_director.fpp.provision as P
    import haunt_director.store as S
    d = tmp_path / "data"
    S.ensure_data_dir(d)
    monkeypatch.setattr(S, "DATA_DIR", d)
    monkeypatch.setattr(A, "Store", lambda: S.Store(d))
    monkeypatch.setattr(P, "ensure_all", lambda *a, **k: {"changed": False})
    sent = []

    class T:
        name = "test"
        def open(self): pass
        def send(self, frame, now): sent.append(frame); return 1
        def close(self, zero_frame=None): pass
    monkeypatch.setattr(A, "make_transport", lambda s: T())
    with TestClient(A.app) as c:
        yield c, A, d, sent


def test_add_a_light_past_channel_64_and_it_lights_up(app_in):
    c, A, d, sent = app_in
    r = c.get("/api/rig").json()
    assert r["block"]["end"] == 64 and r["issues"] == [] and not r["busy"]
    assert any(p["id"] == "generic-rgbw-4ch" and not p["tested"] for p in r["profiles"])
    r = c.post("/api/rig/devices", json={"name": "Porch Light", "profile_id": "holdlamp-rgbw-8ch", "follows": "light_skelly"})
    assert r.status_code == 200, r.text
    dev = r.json()["device"]
    assert dev["id"] == "porch_light" and dev["dmx_address"] == 65 and dev["follows"] == "light_skelly"
    assert dev["sim_position"] and dev["aim"]
    assert r.json()["rig"]["block"]["end"] == 72
    assert json.loads((d / "devices.json").read_text())[-1]["id"] == "porch_light"     # saved
    assert any(x["id"] == "porch_light" for x in c.get("/api/devices").json())
    assert c.post("/api/devices/porch_light/set", json={"rgb": [0, 255, 0], "brightness": 255}).status_code == 200
    time.sleep(0.15)
    assert len(sent[-1]) == 72 and sent[-1][64] == 255 and sent[-1][66] == 255      # on the wire at 65+
    c.post("/api/devices/porch_light/release")
    sid = next(s["id"] for s in c.get("/api/scenes").json()
               if (A.app.state.store.scenes[s["id"]].tracks.get("light_skelly") and
                   not A.app.state.store.scenes[s["id"]].tracks.get("porch_light")))
    st = c.get(f"/api/scenes/{sid}/state", params={"t": 2}).json()["devices"]
    assert st["porch_light"] == st["light_skelly"]                  # it joins a scene written before it existed
    assert "porch_light" in c.get("/api/house").json()["light_order"]


def test_the_api_refuses_broken_changes_and_waits_for_the_show(app_in):
    c, A, d, sent = app_in
    r = c.post("/api/rig/devices", json={"name": "Clash", "profile_id": "holdlamp-rgbw-8ch", "dmx_address": 20})
    assert r.status_code == 400 and "overlaps" in r.json()["detail"]
    r = c.post("/api/rig/devices", json={"name": "Wrong", "kind": "dmx_fog", "profile_id": "holdlamp-rgbw-8ch"})
    assert r.status_code == 400 and "no fog channel" in r.json()["detail"]
    assert c.post("/api/rig/devices", json={"name": "No profile"}).status_code == 400
    assert c.put("/api/rig/devices/light_path", json={"dmx_address": 1}).status_code == 400
    r = c.put("/api/rig/devices/light_skelly", json={"dmx_address": 20})    # moving the EARLIER light onto a later one
    assert r.status_code == 400 and "overlaps" in r.json()["detail"]
    assert c.put("/api/rig/devices/nope", json={"name": "x"}).status_code == 404
    A.app.state.show.armed = True
    try:
        r = c.post("/api/rig/devices", json={"name": "Later", "profile_id": "holdlamp-rgbw-8ch"})
        assert r.status_code == 409 and "Disarm" in r.json()["detail"]
        assert c.post("/api/rig/devices/light_path/identify").status_code == 409
    finally:
        A.app.state.show.armed = False
    assert not any(x["id"] in ("clash", "wrong", "later") for x in c.get("/api/devices").json())


def test_edit_flash_and_delete(app_in):
    c, A, d, sent = app_in
    r = c.put("/api/rig/devices/light_path", json={"name": "Tree Uplight", "dmx_address": 97, "follows": "light_skelly"})
    assert r.status_code == 200, r.text
    assert r.json()["device"]["name"] == "Tree Uplight" and r.json()["rig"]["block"]["end"] == 104
    r = c.post("/api/rig/devices/light_path/identify")
    assert r.status_code == 200
    time.sleep(0.15)
    assert sent[-1][96] == 255 and sent[-1][97] == 255 and sent[-1][98] == 255   # white at the new address
    assert c.post("/api/rig/devices/fog_main/identify").status_code == 400
    r = c.put("/api/rig/devices/light_path", json={"follows": ""})
    assert r.json()["device"]["follows"] is None
    c.put("/api/rig/devices/light_porch", json={"follows": "light_skelly"})
    r = c.delete("/api/rig/devices/light_skelly")
    assert r.status_code == 200 and r.json()["unfollowed"] == ["House Light"]
    ids = [x["id"] for x in c.get("/api/devices").json()]
    assert "light_skelly" not in ids
    assert c.get("/api/rig").json()["issues"] == []                 # no dangling follows left behind
    r = c.post("/api/rig/devices", json={"name": "Left Spot", "kind": "static_rgb"})
    assert r.status_code == 200 and r.json()["device"]["kind"] == "static_rgb" and "dmx_address" not in {
        k for k, v in r.json()["device"].items() if v is not None}


def test_the_system_tab_reports_the_rig(app_in):
    c, A, d, sent = app_in
    chk = {x["id"]: x for x in c.get("/api/system/checks").json()["checks"]}
    assert chk["rig"]["status"] == "ok" and "3 lights and 1 fogger" in chk["rig"]["detail"]
    devs = json.loads((d / "devices.json").read_text())
    devs.append({"id": "bad", "name": "Bad", "kind": "dmx_fixture", "profile_id": "holdlamp-rgbw-8ch", "dmx_address": 3})
    A.app.state.store.replace_devices([Device.model_validate(x) for x in devs])
    chk = {x["id"]: x for x in c.get("/api/system/checks").json()["checks"]}
    assert chk["rig"]["status"] == "fail" and "overlaps" in chk["rig"]["detail"]


def test_the_page_has_the_rig_editor():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "web" / "index.html").read_text()
    assert 'id="rigCard"' in html and "rig.js" in html
    js = (root / "web" / "rig.js").read_text()
    for route in ("/api/rig", "/api/rig/devices", "/identify", "suggest-address"):
        assert route in js


FOLLOW_RUNNER = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {window: {}, document: {addEventListener(){}, querySelectorAll(){ return []; }},
             performance: {now: () => 0}, requestAnimationFrame(){ return 0; }, cancelAnimationFrame(){},
             localStorage: {getItem(){ return null; }, setItem(){}}, console};
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[process.argv.length - 1], "utf8"), ctx);
const job = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(job.cases.map(c => c.times.map(t => ctx.simEvaluate(c.scene, t, 0, job.follows)))));
"""


def test_the_simulator_borrows_parts_exactly_like_the_rig():
    """sim.js must give followers the same part the rig does (web/sim.js partSource)."""
    import random
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("needs node")
    root = Path(__file__).resolve().parents[1]
    rng = random.Random(11)
    follows = {"c": "a", "d": "c", "e": "f", "f": "e", "g": "nobody"}
    cases = []
    for i in range(80):
        tracks = {}
        for dev in "abcdefg":
            r = rng.random()
            if r < 0.4:
                continue                                    # no part at all
            if r < 0.5:
                tracks[dev] = []                            # an emptied part
                continue
            tracks[dev] = [{"id": f"{dev}{k}", "t": round(rng.uniform(0, 9), 2), "type": "set_color",
                            "params": {"color": "#%06x" % rng.randint(0, 0xffffff), "brightness": round(rng.random(), 3)}}
                           for k in range(rng.randint(1, 4))]
        cases.append({"scene": {"id": f"r{i}", "name": "r", "duration_s": 10, "tracks": tracks},
                      "times": [0.0, 2.5, 5.0, 9.9]})
    js = subprocess.run([node, "-e", FOLLOW_RUNNER, str(root / "web" / "sim.js")],
                        input=json.dumps({"cases": cases, "follows": follows}),
                        capture_output=True, text=True, timeout=60, check=True)
    got = json.loads(js.stdout)
    borrowed = 0
    for c, frames in zip(cases, got):
        sc = Scene.model_validate(c["scene"])
        for t, fr in zip(c["times"], frames):
            want = {k: "#%02x%02x%02x" % v.rgb for k, v in evaluate(sc, t, follows).items()}
            assert {k: v["hex"] for k, v in fr.items()} == want, (c["scene"]["id"], t)
            borrowed += sum(1 for k in want if not sc.tracks.get(k))
    assert borrowed > 50
