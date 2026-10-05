"""Longer generated scenes and Extend.

A scene longer than a minute is written in parts (an outline for the arc, then
each part in its own window), the music clip follows the scene length, and
Extend continues an existing scene without touching the part that's already
there. YouTube downloads never clobber a full import other scenes use."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from haunt_director import generate as G

DEVICES = {"light_a": "dmx_fixture", "light_b": "dmx_fixture", "fog_main": "dmx_fog"}
HAVE_FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ---------------- budgets and parts ----------------
def test_parts_and_budgets_scale_with_length():
    assert G.sections_for(30) == [(0.0, 30.0)]
    assert G.sections_for(60) == [(0.0, 60.0)]
    assert G.sections_for(150) == [(0.0, 50.0), (50.0, 100.0), (100.0, 150.0)]
    assert len(G.sections_for(300)) == 5 and G.sections_for(300)[-1] == (240.0, 300.0)
    assert G.event_budget(30) == (12, 36)          # about what a 30 s scene always got
    assert G.event_budget(60) == (24, 72)
    assert G.fog_bursts_max(90) == 6 and G.fog_bursts_max(300) == 10
    assert G.events_cap(30) == 120 and G.events_cap(300) == 450


def test_sanitize_window_keeps_a_part_inside_its_slot():
    raw = {"tracks": {"light_a": [
        {"t": 5, "type": "set_color", "params": {"color": "#ff0000"}},
        {"t": 62, "type": "fade_color", "duration_s": 30, "params": {"color": "#00ff00"}},
        {"t": 130, "type": "set_color", "params": {"color": "#0000ff"}}]}}
    tr, _, _ = G.sanitize_scene(raw, DEVICES, 180, "flat", window=(60, 120))
    evs = tr["light_a"]
    assert [e["t"] for e in evs] == [62.0]
    assert evs[0]["duration_s"] == 30.0
    raw["tracks"]["light_a"][1]["duration_s"] = 90          # would run past the part
    tr, _, _ = G.sanitize_scene(raw, DEVICES, 180, "flat", window=(60, 120))
    assert tr["light_a"][0]["duration_s"] == 58.0


def test_part_counted_from_zero_is_shifted_into_place():
    raw = {"tracks": {"light_a": [{"t": 0}, {"t": 10}, {"t": 55}]}}
    G.normalize_part_times(raw, 60, 120)
    assert [e["t"] for e in raw["tracks"]["light_a"]] == [60, 70, 115]
    ok = {"tracks": {"light_a": [{"t": 61}, {"t": 90}]}}
    G.normalize_part_times(ok, 60, 120)
    assert [e["t"] for e in ok["tracks"]["light_a"]] == [61, 90]


# ---------------- a fake model ----------------
class FakeLLM:
    """Answers the outline and each part; can misbehave on request."""
    def __init__(self, relative_parts=(), garbage=None):
        self.calls, self.relative, self.garbage = [], set(relative_parts), dict(garbage or {})

    def __call__(self, system, user, api_key, model, provider="anthropic", **kw):
        self.calls.append(user)
        lights = re.findall(r'- "([^"]+)"  \(RGBW light\)', user) or ["light_a", "light_b"]
        fogs = re.findall(r'- "([^"]+)"  \(FOG MACHINE\)', user) or ["fog_main"]
        la, lb, fg = lights[0], lights[1 % len(lights)], fogs[0]
        if user.startswith("Plan a"):
            if self.garbage.get("outline", 0) > 0:
                self.garbage["outline"] -= 1
                return "no json here"
            n = len(re.findall(r"- Part \d+:", user))
            return json.dumps({"name": "Long Night", "blurb": "It keeps coming.",
                               "parts": [{"story": f"beat {i}", "opens": "#110022 dim", "closes": "#330044"}
                                         for i in range(n)]})
        m = re.search(r"part (\d+) of (\d+).*?from t=([\d.]+) s to t=([\d.]+) s", user, re.S)
        if m:
            k = int(m.group(1)); a, b = float(m.group(3)), float(m.group(4))
            if self.garbage.get(k, 0) > 0:
                self.garbage[k] -= 1
                return '{"tracks": {"light_a": [ {"t": 1'          # truncated
            off = 0.0 if k in self.relative else a
            span = b - a
            ev = lambda t, typ, **kw: dict({"t": off + t, "type": typ}, **kw)
            return json.dumps({"tracks": {
                la: [ev(0.5, "fade_color", duration_s=span, params={"color": "#2e0854", "brightness": 0.6}),
                     ev(span * 0.5, "strobe", duration_s=2, params={"color": "#ffffff", "rate_hz": 40})],
                lb: [ev(1.0, "pulse", duration_s=span - 2, params={"color": "#39ff14", "rate_hz": 2})],
                fg: [ev(2.0, "fog", duration_s=20, params={"intensity": 1}),
                     ev(span - 5, "fog", duration_s=3, params={"intensity": 0.5})]}})
        if user.startswith("Continue an existing"):
            a = float(re.search(r"between ([\d.]+) and ([\d.]+)", user).group(1))
            return json.dumps({"tracks": {la: [{"t": a + 1, "type": "fade_color", "duration_s": 5,
                                                "params": {"color": "#ff5a1e", "brightness": 0.8}}],
                                          lb: [{"t": a + 3, "type": "set_color",
                                                "params": {"color": "#8b2fff", "brightness": 0.5}}]}})
        # a short, single-call scene
        return json.dumps({"name": "Short", "blurb": "b", "tracks": {
            la: [{"t": 1, "type": "set_color", "params": {"color": "#ff0000", "brightness": 1}}]}})


def test_short_scene_is_still_one_call(monkeypatch):
    fake = FakeLLM(); monkeypatch.setattr(G, "call_llm", fake)
    tr, name, _ = G.generate("a quick hit", 45, "flat", DEVICES, "k", "m")
    assert len(fake.calls) == 1 and name == "Short" and tr["light_a"]
    assert "roughly 18-54 events" in fake.calls[0]


def test_long_scene_outline_then_parts_merged_safely(monkeypatch):
    fake = FakeLLM(relative_parts={2}); monkeypatch.setattr(G, "call_llm", fake)
    tr, name, blurb = G.generate("a long graveyard night", 180, "slow_build", DEVICES, "k", "m")
    assert len(fake.calls) == 4 and fake.calls[0].startswith("Plan a")        # outline + 3 parts
    assert (name, blurb) == ("Long Night", "It keeps coming.")
    a = tr["light_a"]
    fades = sorted(e["t"] for e in a if e["type"] == "fade_color")
    assert fades == [0.5, 60.5, 120.5]                                         # part 2 was shifted
    assert all(0 <= e["t"] < 180 for evs in tr.values() for e in evs)
    for e in a:                                                                # nothing runs into the next part
        if e["type"] == "fade_color":
            assert e["t"] + e["duration_s"] <= (e["t"] // 60 + 1) * 60 + 1e-6
    assert all(e["params"]["rate_hz"] <= G.STROBE_HZ_MAX for e in a if e["type"] == "strobe")
    fog = [e for e in tr["fog_main"] if e["type"] == "fog"]
    assert len(fog) <= G.fog_bursts_max(180) and all(e["duration_s"] <= G.FOG_BURST_MAX_S for e in fog)
    ids = [e["id"] for evs in tr.values() for e in evs]
    assert len(ids) == len(set(ids))
    early = next(e for e in a if e["t"] == 0.5)["params"]["brightness"]
    late = next(e for e in a if e["t"] == 120.5)["params"]["brightness"]
    assert early < late                                                        # slow build applied once, scene-wide
    assert "part 2 of 3" in fake.calls[2] or "part 2 of 3" in "".join(fake.calls)
    assert "beat 1" in fake.calls[1]                                           # every part sees the whole arc


def test_a_flaky_part_is_retried_and_a_dead_one_fails(monkeypatch):
    fake = FakeLLM(garbage={2: 1}); monkeypatch.setattr(G, "call_llm", fake)
    tr, _, _ = G.generate("x", 120, "flat", DEVICES, "k", "m")
    assert len(fake.calls) == 4 and any(e["t"] >= 60 for e in tr["light_a"])
    fake = FakeLLM(garbage={1: 2}); monkeypatch.setattr(G, "call_llm", fake)
    with pytest.raises(ValueError):
        G.generate("x", 120, "flat", DEVICES, "k", "m")


def test_a_bad_outline_still_makes_the_scene(monkeypatch):
    fake = FakeLLM(garbage={"outline": 2}); monkeypatch.setattr(G, "call_llm", fake)
    tr, name, _ = G.generate("x", 90, "flat", DEVICES, "k", "m")
    assert tr["light_a"] and name is None


# ---------------- audio helpers (real ffmpeg) ----------------
def _tone(path, seconds, seed=7):
    """Deterministic, non-repeating test audio (pink noise)."""
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-i",
                    f"anoisesrc=color=pink:seed={seed}:duration={seconds}:sample_rate=22050",
                    "-ac", "1", "-c:a", "libmp3lame", "-q:a", "4", str(path)], check=True)


@pytest.mark.skipif(not HAVE_FF, reason="needs ffmpeg")
def test_autoclip_never_touches_an_existing_full_import(tmp_path, monkeypatch):
    import haunt_director.api.app as A
    full = tmp_path / "yt-abc.mp3"; _tone(full, 30)
    before = full.read_bytes()

    def fake_dl(url, kind, d):
        _tone(Path(d) / "yt-abc.mp3", 30)
        return {"ok": True, "file": "yt-abc.mp3", "title": "Song"}
    monkeypatch.setattr(A, "_yt_download", fake_dl)
    monkeypatch.setattr(A, "_yt_captions", lambda url, d: None)
    r = A.youtube_autoclip("https://youtu.be/abc", 12, "storm", tmp_path)
    assert r["ok"] and r["file"].startswith("yt-abc-t") and abs(r["dur"] - 12) < 0.3
    assert full.read_bytes() == before                                   # the full import is untouched
    assert not list(tmp_path.glob(".clip-*"))                            # scratch folder cleaned up
    assert isinstance(r["start"], float)
    r2 = A.youtube_autoclip("https://youtu.be/abc", 200, "storm", tmp_path)
    assert r2["ok"] and r2["dur"] < 31                                   # shorter song: the whole song


@pytest.mark.skipif(not HAVE_FF, reason="needs ffmpeg")
def test_extend_music_continues_from_the_exact_spot(tmp_path):
    import haunt_director.api.app as A
    from haunt_director.domain.scenes import Scene, SceneAudio
    src = tmp_path / "yt-xyz.mp3"; _tone(src, 40, seed=11)
    # an older clip: really starts at 10.4 s, but its name only says 10
    assert A._ff_cut(src, 10.4, 8.0, tmp_path / "yt-xyz-t10_18.mp3")
    sc = Scene(id="s", name="s", duration_s=8, audio=SceneAudio(music="yt-xyz-t10_18.mp3"))
    start, matched = A._refine_clip_start(tmp_path / "yt-xyz-t10_18.mp3", src, 10.0)
    assert matched and abs(start - 10.4) < 0.03
    r = A.extend_music(sc, 20.0, tmp_path)
    assert r["ok"] and abs(r["start"] - 10.4) < 0.03 and abs(r["dur"] - 20) < 0.3
    assert r["file"] != "yt-xyz-t10_18.mp3" and (tmp_path / "yt-xyz-t10_18.mp3").exists()
    r = A.extend_music(sc, 60.0, tmp_path)                               # past the end of the song
    assert r["ok"] and r["dur"] < 31
    whole = Scene(id="w", name="w", duration_s=8, audio=SceneAudio(music="yt-xyz.mp3"))
    assert A.extend_music(whole, 20.0, tmp_path) == {"ok": False, "reason": "not_extendable"}


# ---------------- the API ----------------
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
    monkeypatch.setattr(G, "resolve_key", lambda s=None: ("k", "anthropic", "m"))
    return A


def test_generate_long_scene_api_saves_metadata(monkeypatch):
    A = _boot(monkeypatch)
    monkeypatch.setattr(G, "call_llm", FakeLLM())
    with TestClient(A.app) as c:
        r = c.post("/api/generate", json={"description": "slow dread", "duration_s": 150,
                                          "intensity_flow": "waves"})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        try:
            sc = c.get(f"/api/scenes/{sid}").json()
            assert sc["duration_s"] == 150 and sc["gen"]["description"] == "slow dread"
            assert sc["gen"]["flow"] == "waves" and sc["gen"]["beat_sync"] is False
            assert max(e["t"] for evs in sc["tracks"].values() for e in evs) > 100
            assert c.post("/api/generate", json={"description": "x", "duration_s": 301}).status_code == 422
        finally:
            c.delete(f"/api/scenes/{sid}")


def test_extend_api_adds_a_part_and_leaves_the_rest(monkeypatch):
    A = _boot(monkeypatch)
    fake = FakeLLM(); monkeypatch.setattr(G, "call_llm", fake)
    with TestClient(A.app) as c:
        sid = c.post("/api/scenes", json={"name": "Ext Test", "duration_s": 40}).json()["id"]
        try:
            tracks = {"light_skelly": [{"id": "a1", "t": 2, "type": "set_color",
                                        "params": {"color": "#c8003c", "brightness": 0.7}}]}
            c.put(f"/api/scenes/{sid}", json={"tracks": tracks})
            assert c.post("/api/scenes/nope/extend", json={"seconds": 30}).status_code == 404
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 30, "note": "build to a big finish"})
            assert r.status_code == 200, r.text
            j = r.json()
            assert j["duration_s"] == 70 and j["added_s"] == 30 and j["events"] >= 1 and not j["beat"]
            assert j["music"] is None
            assert "build to a big finish" in fake.calls[-1] and "#c8003c" in fake.calls[-1]
            sc = c.get(f"/api/scenes/{sid}").json()
            old = [e for e in sc["tracks"]["light_skelly"] if e["t"] < 40]
            assert old == [{"id": "a1", "t": 2.0, "type": "set_color", "duration_s": 0.0,
                            "params": {"color": "#c8003c", "brightness": 0.7}}]
            new = [e for evs in sc["tracks"].values() for e in evs if e["id"] != "a1"]
            assert new and all(40 <= e["t"] < 70 for e in new)
            c.put(f"/api/scenes/{sid}", json={"duration_s": 290})
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 90})
            assert r.status_code == 200 and r.json()["duration_s"] == 300        # only what's left
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 30})
            assert r.status_code == 400 and "5 minutes" in r.json()["detail"]
        finally:
            c.delete(f"/api/scenes/{sid}")


@pytest.mark.skipif(not HAVE_FF, reason="needs ffmpeg")
def test_extend_beat_sync_scene_stays_on_the_beat(monkeypatch):
    A = _boot(monkeypatch)
    import haunt_director.beats as BT
    ad = Path(A.__file__).resolve().parents[2] / A.settings.audio_dir
    old_clip, new_clip = ad / "zz-test-beat-t0_20.mp3", ad / "zz-test-beat-t0_40.mp3"
    _tone(old_clip, 20); _tone(new_clip, 40)
    monkeypatch.setattr(A, "extend_music", lambda sc, nd, d: {"ok": True, "file": new_clip.name, "dur": 40.0,
                                                               "start": 0.0, "url": None, "title": None})
    grid = [round(i * 0.5, 3) for i in range(80)]                         # 120 BPM
    monkeypatch.setattr(BT, "detect_beats", lambda path, max_s=60, **k: {
        "tempo": 120, "beat_period": 0.5, "beats": [b for b in grid if b < max_s],
        "downbeats": [b for b in grid[::4] if b < max_s]})
    with TestClient(A.app) as c:
        sid = c.post("/api/scenes", json={"name": "Beat Ext", "duration_s": 20}).json()["id"]
        try:
            raw = BT.compose_beatsync(BT.detect_beats("x", 20), ["#39ff14", "#8b2fff"], "#ffffff",
                                      ["light_skelly", "light_path", "light_porch"], [], 20)
            c.put(f"/api/scenes/{sid}", json={"tracks": raw, "blurb": "Lights locked to the beat (~120 BPM).",
                                              "audio": {"music": old_clip.name}})
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 20})
            assert r.status_code == 200, r.text
            assert r.json()["beat"] and r.json()["duration_s"] == 40 and r.json()["music"]["extended"]
            sc = c.get(f"/api/scenes/{sid}").json()
            assert sc["audio"]["music"] == new_clip.name and sc["gen"]["clip_file"] == new_clip.name
            new = [e for e in sc["tracks"]["light_skelly"] if e["t"] >= 20]
            pops = [e for e in new if e["type"] == "set_color"]
            assert pops and all(abs(e["t"] / 0.5 - round(e["t"] / 0.5)) < 1e-6 for e in pops)   # on the beat
            flashes = {e["params"]["color"] for e in new if e["type"] == "set_color"
                       and e["params"]["brightness"] >= 0.99}
            assert flashes == {"#ffffff"}                                 # accent read back off the old part
        finally:
            c.delete(f"/api/scenes/{sid}")
            for f in (old_clip, new_clip):
                f.unlink(missing_ok=True)
                (ad / "_wavcache" / (f.stem + ".norm.wav")).unlink(missing_ok=True)
            lib = A._load_audio_lib()                       # don't leave test clips in the library
            for k in [k for k in lib if k.startswith("zz-test-beat")]:
                lib.pop(k)
            A._audio_lib_path().write_text(json.dumps(lib, indent=2))


# ---------------- safety and robustness (from review) ----------------
def test_fog_never_runs_together_and_is_spread_across_the_scene():
    bursts = [{"t": t, "type": "fog", "duration_s": 6, "params": {"intensity": 1}} for t in range(0, 300, 5)]
    tr, _, _ = G.sanitize_scene({"tracks": {"fog_main": bursts}}, DEVICES, 300, "flat")
    fog = tr["fog_main"]
    assert len(fog) <= G.fog_bursts_max(300)
    for a, b in zip(fog, fog[1:]):
        assert b["t"] >= a["t"] + a["duration_s"] + G.FOG_GAP_S          # clear air between bursts
    assert fog[0]["t"] == 0 and fog[-1]["t"] > 200                        # not bunched at the start


def test_strobes_are_capped_and_rested():
    st = [{"t": t, "type": "strobe", "duration_s": 60, "params": {"color": "#ffffff", "rate_hz": 10}}
          for t in (0, 5, 30)]
    tr, _, _ = G.sanitize_scene({"tracks": {"light_a": st}}, DEVICES, 120, "flat")
    s = tr["light_a"]
    assert [e["t"] for e in s] == [0.0, 30.0]                              # 5 s one starts during the first
    assert all(e["duration_s"] <= G.STROBE_MAX_S for e in s)


def test_event_cap_is_shared_fairly_and_late_events_are_dropped():
    many = lambda: [{"t": i * 0.5, "type": "set_color", "params": {"color": "#ff0000"}} for i in range(200)]
    tr, _, _ = G.sanitize_scene({"tracks": {"light_a": many(), "light_b": many()}}, DEVICES, 100, "flat",
                                events_max=100)
    assert len(tr["light_a"]) == len(tr["light_b"]) == 50                  # no fixture starved
    tr, _, _ = G.sanitize_scene({"tracks": {"light_a": [{"t": 41, "type": "blackout"},
                                                        {"t": 10, "type": "blackout"}]}}, DEVICES, 30, "flat")
    assert [e["t"] for e in tr["light_a"]] == [10.0]                       # past the end: dropped, not piled at 30


def test_long_parts_are_told_energy_not_brightness(monkeypatch):
    fake = FakeLLM(); monkeypatch.setattr(G, "call_llm", fake)
    G.generate("x", 300, "cold_open", DEVICES, "k", "m")
    part1 = next(c for c in fake.calls if "part 1 of 5" in c)
    assert "% intensity" not in part1 and "low energy" in part1 and "full 0-1 scale" in part1


def test_malformed_outline_parts_dont_crash(monkeypatch):
    fake = FakeLLM()
    real = fake.__call__

    def odd(system, user, *a, **k):
        if user.startswith("Plan a"):
            fake.calls.append(user)
            return json.dumps({"name": "Odd", "parts": ["just a string", None, 7]})
        return real(system, user, *a, **k)
    monkeypatch.setattr(G, "call_llm", odd)
    tr, name, _ = G.generate("x", 150, "flat", DEVICES, "k", "m")
    assert name == "Odd" and tr["light_a"]


def test_timeouts_get_one_retry_and_a_plain_message(monkeypatch):
    import httpx
    n = {"calls": 0}

    def slow(*a, **k):
        n["calls"] += 1
        raise httpx.ReadTimeout("")
    monkeypatch.setattr(G, "call_llm", slow)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(httpx.ReadTimeout) as ei:
        G.generate("x", 30, "flat", DEVICES, "k", "m")
    assert n["calls"] == 2
    assert G.friendly_error(ei.value).startswith("The AI took too long")


def test_extend_refuses_a_stale_copy_and_an_edit_mid_extend(monkeypatch):
    A = _boot(monkeypatch)
    monkeypatch.setattr(G, "call_llm", FakeLLM())
    with TestClient(A.app) as c:
        sid = c.post("/api/scenes", json={"name": "Race Test", "duration_s": 40}).json()["id"]
        try:
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 30, "from_s": 35})
            assert r.status_code == 409 and "Reopen" in r.json()["detail"]
            real = G.extend_tracks

            def edit_meanwhile(ctx, *a, **k):
                A.app.state.ctrl.store.scenes[sid].blurb = "edited in another tab"
                return real(ctx, *a, **k)
            monkeypatch.setattr(G, "extend_tracks", edit_meanwhile)
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 30, "from_s": 40})
            assert r.status_code == 409 and "edited" in r.json()["detail"]
            assert c.get(f"/api/scenes/{sid}").json()["duration_s"] == 40       # nothing saved
        finally:
            c.delete(f"/api/scenes/{sid}")


@pytest.mark.skipif(not HAVE_FF, reason="needs ffmpeg")
def test_beat_extend_refuses_a_misaligned_clip_and_cleans_up(monkeypatch):
    A = _boot(monkeypatch)
    ad = Path(A.__file__).resolve().parents[2] / A.settings.audio_dir
    old_clip, new_clip = ad / "zz-test-al-t0_20.mp3", ad / "zz-test-al-t0_40.mp3"
    _tone(old_clip, 20)

    def misaligned(sc, nd, d):
        _tone(new_clip, 40)
        return {"ok": True, "file": new_clip.name, "dur": 40.0, "start": 0.3, "url": None, "aligned": False}
    monkeypatch.setattr(A, "extend_music", misaligned)
    with TestClient(A.app) as c:
        sid = c.post("/api/scenes", json={"name": "Align Test", "duration_s": 20}).json()["id"]
        try:
            c.put(f"/api/scenes/{sid}", json={"blurb": "Lights locked to the beat.", "audio": {"music": old_clip.name}})
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 20})
            assert r.status_code == 409 and "drift" in r.json()["detail"]
            assert not new_clip.exists()                                     # the unused clip was removed
            assert c.get(f"/api/scenes/{sid}").json()["duration_s"] == 20
        finally:
            c.delete(f"/api/scenes/{sid}")
            old_clip.unlink(missing_ok=True); new_clip.unlink(missing_ok=True)


@pytest.mark.skipif(not HAVE_FF, reason="needs ffmpeg")
def test_generate_drops_its_clip_when_there_is_nothing_to_save(monkeypatch):
    A = _boot(monkeypatch)
    ad = Path(A.__file__).resolve().parents[2] / A.settings.audio_dir
    clip = ad / "zz-test-orphan-t0_30.mp3"

    def fake_clip(url, target, desc, d, cap=None):
        _tone(clip, 5)
        return {"ok": True, "file": clip.name, "title": "t", "via": "energy", "dur": 5.0, "start": 0.0}
    monkeypatch.setattr(A, "youtube_autoclip", fake_clip)
    monkeypatch.setattr(G, "generate", lambda *a, **k: ({}, None, ""))
    try:
        with TestClient(A.app) as c:
            r = c.post("/api/generate", json={"description": "x", "duration_s": 30,
                                              "youtube_url": "https://youtu.be/x"})
            assert r.status_code == 502 and not clip.exists()
    finally:
        clip.unlink(missing_ok=True)


def test_old_clip_finds_its_video_from_its_name(tmp_path):
    import haunt_director.api.app as A
    from haunt_director.domain.scenes import Scene, SceneAudio
    (tmp_path / "yt--8O4o9VZnKY-t606_636.mp3").write_bytes(b"x")
    (tmp_path / "my-song-t5_12.mp3").write_bytes(b"x")
    sc = Scene(id="s", name="s", audio=SceneAudio(music="yt--8O4o9VZnKY-t606_636.mp3"))
    o = A._music_origin(sc, tmp_path)
    assert o["url"] == "https://www.youtube.com/watch?v=-8O4o9VZnKY" and o["start"] == 606 and not o["exact"]
    other = Scene(id="o", name="o", audio=SceneAudio(music="my-song-t5_12.mp3"))
    assert A._music_origin(other, tmp_path) is None                      # no source, no link: loops
    sneaky = Scene(id="x", name="x", audio=SceneAudio(music="../data/devices.json"))
    assert A._music_origin(sneaky, tmp_path) is None
