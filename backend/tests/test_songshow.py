"""Song show: the band (drums, bass, guitar) drives the lights, the lyrics'
meaning picks the colours. Tested on a synthetic rock track and made-up
lyrics, so no real song or lyric is needed (or stored)."""
import bisect
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from haunt_director import generate as G
from haunt_director import songshow as S

HAVE_FF = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
LIGHTS = ["light_skelly", "light_path", "light_porch"]
DEVICES = {d: "dmx_fixture" for d in LIGHTS} | {"fog_main": "dmx_fog"}


@pytest.fixture(scope="module")
def song(tmp_path_factory):
    if not HAVE_FF:
        pytest.skip("needs ffmpeg")
    from _synth import make
    p = tmp_path_factory.mktemp("song") / "synth.wav"
    beats, truth, end = make(str(p))
    return {"path": p, "beats": beats, "truth": truth, "end": end}


def _near(t, arr, tol):
    i = bisect.bisect_left(arr, t)
    return any(abs(arr[j] - t) <= tol for j in (i - 1, i) if 0 <= j < len(arr))


def _lines(song, bars_words):
    return [(song["beats"][4 * b] - 0.2, w) for b, w in bars_words]


MADE_UP = [(4, "lanterns in the hollow"), (6, "the candles burn down"), (8, "ride the black road"),
           (9, "ride the black road"), (11, "the owls are calling"), (13, "ride the black road"),
           (14, "ride the black road")]


# ---------------- listening ----------------
def test_beats_follow_a_drifting_drummer_and_find_the_band(song):
    lines = _lines(song, MADE_UP)
    m = S.analyze(song["path"], song["end"], [t for t, _ in lines])
    assert 115 <= m["tempo"] <= 119
    band = song["beats"][16:]                                   # once the drums are in
    assert sum(_near(b, m["beats"], 0.05) for b in band) >= 0.95 * len(band)
    assert sum(_near(b, m["downbeats"], 0.05) for b in band[::4]) >= 0.9 * len(band[::4])
    for name, need in (("kick", 0.9), ("snare", 0.9), ("stab", 0.75)):
        det = [s["t"] for s in m["slots"] if s[name]]
        true = song["truth"][name]
        assert sum(_near(t, true, .06) for t in det) >= need * len(det), name          # precision
        assert sum(_near(t, det, .06) for t in true) >= need * len(true), name         # recall
    drums = "".join("D" if b["drums"] else "." for b in m["bars"])
    assert drums.startswith("....D")                            # guitar alone for the first four bars


def test_music_start_skips_silence_but_not_a_sparse_intro(song, tmp_path):
    import subprocess
    lead = tmp_path / "lead.wav"
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi", "-t", "3", "-i", "anullsrc=r=22050:cl=mono",
                    "-i", str(song["path"]), "-filter_complex", "[0][1]concat=n=2:v=0:a=1", str(lead)],
                   check=True, timeout=60)
    first = song["truth"]["stab"][0]                            # the first guitar stab
    assert abs(S.music_start(lead) - (3.0 + first)) < 0.3
    assert S.music_start(song["path"]) <= first


# ---------------- reading ----------------
def test_lrc_titles_and_picking_the_consensus_timing():
    assert S.parse_lrc("[ar:x]\n[00:01.50] one\n[00:03.00][00:30.25] two\n[00:05.00]\n") == \
        [(1.5, "one"), (3.0, "two"), (30.25, "two")]
    assert S.split_title("Some Band - Night Ride (Official Video)") == ("Some Band", "Night Ride")
    assert S.split_title("Night Ride [Remastered 2003]") == ("", "Night Ride")
    lrc = lambda off: "\n".join(f"[00:{10 + 3 * i + off:05.2f}] line {i}" for i in range(12))
    cands = [{"id": 1, "duration": 200.0, "syncedLyrics": lrc(0.0)},
             {"id": 2, "duration": 201.0, "syncedLyrics": lrc(0.1)},
             {"id": 3, "duration": 199.5, "syncedLyrics": lrc(2.5)},        # an outlier timing
             {"id": 4, "duration": 260.0, "syncedLyrics": lrc(0.0)},        # a different recording
             {"id": 5, "duration": 200.0, "instrumental": True, "syncedLyrics": ""}]
    got = S.pick_lyrics(cands, 200.5)
    assert got["id"] in (1, 2) and len(got["lines"]) == 12
    assert S.pick_lyrics(cands[3:], 200.5) is None
    calls = []
    def search(params):
        calls.append(params)
        if "q" in params:
            return cands
        raise RuntimeError("offline")
    assert S.find_lyrics("Some Band", "Night Ride", 200.5, search=search)["id"] in (1, 2)
    assert calls[0] == {"track_name": "Night Ride", "artist_name": "Some Band"}
    assert S.find_lyrics("", "", 200, search=search) is None


def test_sections_from_words(song):
    lines = _lines(song, MADE_UP)
    m = S.analyze(song["path"], song["end"], [t for t, _ in lines])
    secs = S.sections_from_lyrics(lines, m, song["end"])
    kinds = [s["kind"] for s in secs]
    assert kinds[0] == "intro" and "chorus" in kinds and kinds[-1] in ("outro", "chorus", "solo")
    downs = m["downbeats"]
    assert all(s["start"] == 0 or _near(s["start"], downs, 0.01) for s in secs)      # on bar lines
    assert all(a["end"] == b["start"] for a, b in zip(secs, secs[1:]))
    assert secs[-1]["end"] == pytest.approx(song["end"], abs=0.01)
    no_words = S.sections_from_music(m, song["end"])
    assert no_words and no_words[0]["start"] == 0


def test_colors_from_the_ai_or_sensible_defaults():
    secs = [{"start": 0, "end": 10, "kind": "intro", "energy": 0.2, "lines": []},
            {"start": 10, "end": 30, "kind": "chorus", "energy": 0.9, "lines": [0, 1]}]
    lines = [(10.5, "ride the black road"), (20.0, "ride the black road")]
    system, user = S.build_color_prompt("Night Ride", "Some Band", secs, lines, "make it green")
    assert "ride the black road" in user and "Do not quote" in user and "make it green" in user
    out, moments, theme = S.apply_colors(secs, lines, {
        "theme": "Hellfire on the porch", "sections": [{"i": 1, "palette": ["#FF0000", "bad", "#00ff00"],
                                                         "accent": "#ffffff", "energy": 3}],
        "moments": [{"line": 1, "color": "#ff2200", "effect": "blackout"}, {"line": 99, "color": "#ff0000"}]})
    assert out[0]["palette"] == S.DEFAULT_LOOKS["intro"][0]                     # missing -> default look
    assert out[1]["palette"] == ["#ff0000", "#00ff00", "#00ff00"] and out[1]["energy"] == 1.0
    assert moments == [(20.0, "#ff2200", "blackout")] and theme == "Hellfire on the porch"
    out, moments, _ = S.apply_colors(secs, lines, "garbage")
    assert out[1]["palette"] == S.DEFAULT_LOOKS["chorus"][0] and moments == []


# ---------------- playing ----------------
def test_roles_follow_the_rig():
    assert S.roles_for(LIGHTS) == {"light_skelly": "drums", "light_path": "bass", "light_porch": "guitar"}
    assert S.roles_for(["a", "b"]) == {"a": "drums", "b": "guitar"}
    assert S.roles_for(["a"]) == {"a": "drums+guitar"}
    assert S.roles_for(["a", "b", "c", "d", "e"])["d"] == "guitar"


def test_the_show_plays_the_band_and_stays_safe(song):
    lines = _lines(song, MADE_UP)
    res = S.build(song["path"], song["end"], lines, "Night Ride", "Some Band", LIGHTS, ["fog_main"],
                  ask_json=lambda s, u: {"sections": [], "moments": [{"line": 2, "color": "#00ff66"}]})
    assert res["lyrics_used"] and res["colors_from_ai"] and 115 <= res["tempo"] <= 119
    tr = res["tracks"]
    snares = [e["t"] for e in tr["light_skelly"] if e["type"] == "set_color"
              and e["params"]["brightness"] == 1.0 and e["params"]["color"] in ("#ffffff",)]
    assert sum(_near(t, song["truth"]["snare"], 0.06) for t in snares) >= 0.8 * len(song["truth"]["snare"])
    assert any(e["type"] == "pulse" for e in tr["light_path"])                   # the bass throbs
    assert any(e["params"].get("color") == "#00ff66" for e in tr["light_porch"])  # the accent line
    assert "fog_main" in tr
    clean, _, _ = G.sanitize_scene({"tracks": tr}, DEVICES, song["end"], "flat", 8.0, events_max=2000)
    fog = [e for e in clean.get("fog_main", []) if e["type"] == "fog"]
    assert all(e["duration_s"] <= G.FOG_BURST_MAX_S for e in fog)
    assert all(0 <= e["t"] <= song["end"] for evs in clean.values() for e in evs)
    last = max(e["t"] for e in clean["light_skelly"])
    assert last > song["end"] - 3                                                # an ending, not a cut-off


# ---------------- the app ----------------
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


def _cut(src, dst, seconds=None):
    import subprocess
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-i", str(src)] + (["-t", str(seconds)] if seconds else [])
                   + ["-c:a", "libmp3lame", "-q:a", "4", str(dst)], check=True)


def test_song_show_api_saves_times_and_colours_but_never_the_words(monkeypatch, song):
    A = _boot(monkeypatch)
    ad = Path(A.__file__).resolve().parents[2] / A.settings.audio_dir
    clip, whole = ad / "zz-test-song-t0_30.mp3", ad / "zz-test-song-t0_50.mp3"
    _cut(song["path"], clip, 30)
    _cut(song["path"], whole)
    monkeypatch.setattr(A, "youtube_songclip", lambda url, target, d: {
        "ok": True, "file": clip.name, "title": "Some Band - Night Ride (Official Video)", "start": 0.0,
        "dur": 30.0, "full_dur": song["end"]})
    seen = {}
    monkeypatch.setattr(S, "find_lyrics", lambda artist, track, n, **k: seen.update(a=artist, t=track) or
                        {"id": 7, "lines": _lines(song, MADE_UP)})
    monkeypatch.setattr(S, "lyrics_by_id", lambda lid, **k: _lines(song, MADE_UP) if lid == 7 else [])
    monkeypatch.setattr(G, "call_llm", lambda system, user, *a, **k: json.dumps(
        {"theme": "Cold road", "sections": [{"i": 0, "palette": ["#112233", "#223344", "#334455"],
                                              "accent": "#ffffff", "energy": 0.4}], "moments": []}))
    with TestClient(A.app) as c:
        r = c.post("/api/generate", json={"description": "", "duration_s": 30, "youtube_url": "https://youtu.be/x",
                                          "song_show": True})
        assert r.status_code == 200, r.text
        j = r.json()
        sid = j["id"]
        try:
            assert seen == {"a": "Some Band", "t": "Night Ride"} and j["name"] == "Night Ride"
            assert j["song"]["lyrics"] and j["song"]["colors_from_ai"] and not j["song"]["whole_song"]
            raw = (Path(A.__file__).resolve().parents[2] / "data" / "scenes" / f"{sid}.json").read_text()
            for _, words in MADE_UP:
                assert words not in raw                                           # no lyrics stored
            sc = c.get(f"/api/scenes/{sid}").json()
            g = sc["gen"]
            assert g["mode"] == "song" and g["lyrics"]["lines"] == len(MADE_UP)
            assert g["sections"][0]["palette"] == ["#55aaff", "#80bfff", "#99ccff"]      # the AI's hues, at full strength
            assert "the lyrics" in sc["blurb"] and len(g["downbeats"]) > 8
            end_ids = set(g["ending"]["ids"])
            assert end_ids and 20 < g["ending"]["from"] < 30
            # By hand: a light event in the old ending, and two lanes cleared.
            frm = g["ending"]["from"]
            tracks = {d: evs for d, evs in sc["tracks"].items() if d not in ("light_path", "fog_main")}
            tracks["light_porch"].append({"id": "hand1", "t": round(frm + 0.3, 2), "type": "set_color",
                                          "duration_s": 0, "params": {"color": "#123456", "brightness": 1.0}})
            assert c.put(f"/api/scenes/{sid}", json={"tracks": tracks}).status_code == 200
            monkeypatch.setattr(A, "extend_music", lambda s, nd, d: {"ok": True, "file": whole.name,
                                                                      "dur": song["end"], "start": 0.0, "url": None})
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 30})
            assert r.status_code == 200, r.text
            sc2 = c.get(f"/api/scenes/{sid}").json()
            assert sc2["duration_s"] == pytest.approx(song["end"], abs=0.05)
            ids = {e["id"] for evs in sc2["tracks"].values() for e in evs}
            assert not (ids & end_ids) and "hand1" in ids                     # old ending lifted, your edit kept
            assert not sc2["tracks"].get("light_path") and not sc2["tracks"].get("fog_main")   # cleared stays clear
            sk = sc2["tracks"]["light_skelly"]
            assert max(e["t"] for e in sk) > song["end"] - 3                  # a new ending at the new end
            assert all(e["t"] > 40 for e in sk if e["type"] == "strobe")      # no ending strobe mid-song
            assert any(frm <= e["t"] < 30 for e in sk)                        # the song carries on through the seam
            g2 = sc2["gen"]
            assert g2["ending"]["from"] > 40 and g2["sections"][0] == g["sections"][0]
            assert g2["sections"][-1]["end"] == pytest.approx(song["end"], abs=0.05)
            assert set(g2["roles"]) == {"light_skelly", "light_porch"}
        finally:
            c.delete(f"/api/scenes/{sid}")
            for f in (clip, whole):
                f.unlink(missing_ok=True)
                (ad / "_wavcache" / (f.stem + ".norm.wav")).unlink(missing_ok=True)
            lib = A._load_audio_lib()
            for k in [k for k in lib if k.startswith("zz-test")]:
                lib.pop(k)
            A._audio_lib_path().write_text(json.dumps(lib, indent=2))


def test_extend_after_shortening_and_for_an_untagged_song_show(monkeypatch, song):
    A = _boot(monkeypatch)
    ad = Path(A.__file__).resolve().parents[2] / A.settings.audio_dir
    clip, whole = ad / "zz-test-song2-t0_30.mp3", ad / "zz-test-song2-t0_50.mp3"
    _cut(song["path"], clip, 30)
    _cut(song["path"], whole)
    monkeypatch.setattr(A, "youtube_songclip", lambda url, target, d: {
        "ok": True, "file": clip.name, "title": "Some Band - Night Ride", "start": 0.0, "dur": 30.0,
        "full_dur": song["end"]})
    monkeypatch.setattr(S, "find_lyrics", lambda *a, **k: None)                 # sections from the music
    monkeypatch.setattr(A, "extend_music", lambda s, nd, d: {"ok": True, "file": whole.name,
                                                              "dur": song["end"], "start": 0.0, "url": None})
    monkeypatch.setattr(G, "call_llm", lambda *a, **k: json.dumps({"sections": [{"i": 99}]}))
    strobes_ok = lambda sc: all(e["t"] >= sc["gen"]["ending"]["from"] - 0.02
                                for evs in sc["tracks"].values() for e in evs if e["type"] == "strobe")
    with TestClient(A.app) as c:
        r = c.post("/api/generate", json={"description": "", "duration_s": 30, "youtube_url": "https://youtu.be/x",
                                          "song_show": True})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        try:
            sc = c.get(f"/api/scenes/{sid}").json()
            late = {e["id"] for evs in sc["tracks"].values() for e in evs if e["t"] >= 24}
            assert late
            # the length slider: shorter, with the events past the new end still stored
            assert c.put(f"/api/scenes/{sid}", json={"duration_s": 24}).status_code == 200
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 10})
            assert r.status_code == 200, r.text
            sc = c.get(f"/api/scenes/{sid}").json()
            ids = {e["id"] for evs in sc["tracks"].values() for e in evs}
            assert sc["duration_s"] == pytest.approx(34, abs=0.05) and not (ids & late)   # nothing hidden comes back
            assert strobes_ok(sc) and sc["gen"]["ending"]["from"] > 28
            # a song show with no record of its ending: the last bars are re-laid
            store = A._ctrl(A.app).store
            store.scenes[sid].gen.pop("ending")
            old_end = {e["id"] for evs in sc["tracks"].values() for e in evs if e["t"] >= 30}
            r = c.post(f"/api/scenes/{sid}/extend", json={"seconds": 10})
            assert r.status_code == 200, r.text
            sc = c.get(f"/api/scenes/{sid}").json()
            ids = {e["id"] for evs in sc["tracks"].values() for e in evs}
            assert sc["duration_s"] == pytest.approx(44, abs=0.05) and not (ids & old_end)
            assert strobes_ok(sc) and sc["gen"]["ending"]["from"] > 34                   # past the old end
        finally:
            c.delete(f"/api/scenes/{sid}")
            for f in (clip, whole):
                f.unlink(missing_ok=True)
                (ad / "_wavcache" / (f.stem + ".norm.wav")).unlink(missing_ok=True)
            lib = A._load_audio_lib()
            for k in [k for k in lib if k.startswith("zz-test")]:
                lib.pop(k)
            A._audio_lib_path().write_text(json.dumps(lib, indent=2))


def test_song_show_needs_a_link(monkeypatch):
    A = _boot(monkeypatch)
    with TestClient(A.app) as c:
        r = c.post("/api/generate", json={"description": "", "song_show": True})
        assert r.status_code == 400 and "YouTube" in r.json()["detail"]
        assert c.post("/api/generate", json={"description": ""}).status_code == 400


def test_light_timing_shifts_the_lights_and_persists(monkeypatch):
    A = _boot(monkeypatch)
    import asyncio, time as _t
    from haunt_director.domain.scenes import Scene, Event
    with TestClient(A.app) as c:
        before = c.get("/api/output").json()["light_delay_ms"]
        try:
            assert c.post("/api/output", json={"light_delay_ms": 301}).status_code == 422
            assert c.post("/api/output", json={"light_delay_ms": 120}).json()["light_delay_ms"] == 120
            p = A.app.state.player
            assert p.light_delay_s == pytest.approx(0.12)
            sc = Scene(id="lag", name="lag", duration_s=5, tracks={"light_skelly": [
                Event(id="a", t=0, type="set_color", params={"color": "#ff0000", "brightness": 1.0}),
                Event(id="b", t=1.0, type="set_color", params={"color": "#0000ff", "brightness": 1.0})]})
            portal = c.portal
            portal.call(p.play, sc)
            run = p._run
            frame_at = lambda dt: A.app.state.bus.compose(run.start + dt)
            f = frame_at(1.05)                                                 # [0] dimmer [1] red [3] blue
            assert f[1] == 255 and f[3] == 0                                   # still red: lights 120 ms behind
            assert frame_at(1.2)[3] == 255                                     # blue once the delay has passed
            portal.call(p.stop)
        finally:
            c.post("/api/output", json={"light_delay_ms": before})
    with TestClient(A.app) as c:
        assert c.get("/api/output").json()["light_delay_ms"] == before


def test_sync_test_guards_and_plays(monkeypatch, tmp_path):
    A = _boot(monkeypatch)
    with TestClient(A.app) as c:
        A.app.state.show.armed = True
        assert c.post("/api/output/sync-test").status_code == 409
        A.app.state.show.armed = False
        import threading
        for th in threading.enumerate():                          # let the real audio start-up finish first
            if th.name == "audio-init":
                th.join(15)

        class Eng:
            available = True
            audio_dir = tmp_path
        monkeypatch.setattr(A.app.state, "audio", Eng(), raising=False)
        if HAVE_FF:
            r = c.post("/api/output/sync-test")
            assert r.status_code == 200, r.text
            assert (tmp_path / A.SYNC_CLICK).exists()
            st = A.app.state.player.status
            assert st["playing"] and st["scene"] == "__sync_test"
            assert c.post("/api/output/sync-test").status_code == 409                # already playing
            c.post("/api/play/stop")
            # a light held by a Live card would hide the flash: use another one
            first = r.json()["light"]
            A.app.state.bus.set_manual(first, {1: 255})
            try:
                r = c.post("/api/output/sync-test")
                assert r.status_code == 200 and r.json()["light"] != first
                c.post("/api/play/stop")
            finally:
                A.app.state.bus.release_manual(first)


def test_song_show_needs_a_light(monkeypatch):
    A = _boot(monkeypatch)
    with TestClient(A.app) as c:
        store = A._ctrl(A.app).store
        keep = store.devices
        store.devices = {k: v for k, v in keep.items() if v.kind.value != "dmx_fixture"}
        try:
            r = c.post("/api/generate", json={"description": "", "song_show": True,
                                              "youtube_url": "https://youtu.be/x"})
            assert r.status_code == 400 and "light" in r.json()["detail"]
        finally:
            store.devices = keep


def test_wav_cache_prune_only_drops_copies_of_missing_clips(tmp_path):
    from haunt_director.audio import AudioEngine
    eng = AudioEngine.__new__(AudioEngine)                    # no mixer needed
    eng.audio_dir, eng.cache_dir = tmp_path, tmp_path / "_wavcache"
    eng.cache_dir.mkdir()
    (tmp_path / "kept.v2.mp3").write_bytes(b"x")
    for n in ("kept.v2.norm.wav", "gone.norm.wav", "notes.txt"):
        (eng.cache_dir / n).write_bytes(b"x")
    assert eng.prune_cache() == 1
    assert sorted(p.name for p in eng.cache_dir.iterdir()) == ["kept.v2.norm.wav", "notes.txt"]
    (tmp_path / "kept.v2.mp3").unlink()
    assert eng.prune_cache() == 0                             # an empty audio folder: hands off


# ---------------- from the review ----------------
def test_busy_scenes_are_thinned_evenly_so_the_ending_stays():
    import haunt_director.api.app as A
    many = [{"t": i * 0.05, "type": "set_color", "params": {"color": "#ff0000"}} for i in range(2000)]
    tr, _, _ = G.sanitize_scene({"tracks": {"light_skelly": many}}, DEVICES, 100, "flat", events_max=500)
    ts = [e["t"] for e in tr["light_skelly"]]
    assert len(ts) == 500 and ts[0] == 0 and ts[-1] > 99                       # sparser, not cut off
    assert A._beat_events_cap(300, 4) >= 300 * 4 * 5                           # room for a busy drummer
    assert A._beat_events_cap(300, 50) == A.BEAT_EVENTS_MAX


def test_the_ending_and_the_accents_play_out_and_the_guitar_keeps_up(song):
    from haunt_director.domain.scenes import Scene, Event, evaluate
    sm = S.analyze(song["path"], song["end"])
    secs, _, _ = S.apply_colors(S.sections_from_music(sm, song["end"]), [], None)
    secs[-1]["energy"] = 0.9                                                    # goes out loud: a strobe run-up
    dark_at, flash_at = sm["downbeats"][10], sm["downbeats"][14]
    tr, _ = S.compose(sm, secs, [(dark_at, "#00ff00", "blackout"), (flash_at, "#0000ff", "flash")],
                      LIGHTS, [], song["end"])
    tf, sf = S.ending_plan(sm, secs, song["end"])
    assert sf is not None and S.ending_from(sm, secs, song["end"]) == sf
    sc = Scene(id="x", name="x", duration_s=song["end"],
               tracks={d: [Event(id=str(i), **e) for i, e in enumerate(v)] for d, v in tr.items()})
    beat = sm["period_s"]
    at = lambda t: evaluate(sc, t)["light_skelly"]
    frames = [sf + 0.02 + i * 0.04 for i in range(int((tf - sf - 0.04) / 0.04))]
    assert all(at(t).strobe_hz > 0 for t in frames)                             # two whole bars of strobe
    frames = [dark_at - beat + 0.02 + i * 0.04 for i in range(int((beat - 0.04) / 0.04))]
    assert all(at(t).brightness == 0 for t in frames)                           # a real beat of darkness
    assert at(flash_at + 0.01).rgb == (0, 0, 255)
    assert at(song["end"] - 0.05).brightness == 0                               # and out to dark
    # busy strumming: a stab on every 16th still plays the guitar light on the 8ths
    slots = [{"t": i * 0.125, "beat": i // 4, "pos": i % 16, "kick": False, "snare": False, "stab": True,
              "stab_s": 0.8, "bass": 0.0, "mid": 0.8, "air": 0.0, "drums": False} for i in range(128)]
    fake = {"period_s": 0.5, "beats": [i * 0.5 for i in range(32)], "downbeats": [i * 2.0 for i in range(8)],
            "bars": [{"t": i * 2.0, "end": i * 2.0 + 2, "energy": 0.5, "drums": False} for i in range(8)],
            "slots": slots}
    sec = [{"start": 0.0, "end": 16.0, "kind": "verse", "palette": ["#ff0000", "#00ff00", "#0000ff"],
            "accent": "#ffffff", "energy": 0.5}]
    tr2, _ = S.compose(fake, sec, [], LIGHTS, [], 16.0)
    assert sum(1 for e in tr2["light_porch"] if e["type"] == "set_color") >= 50   # (the last bars are the ending)
    assert S.compose(sm, [dict(secs[0], kind="solo")], [], [], ["fog_main"], song["end"])[1] == {}   # no lights


def test_malformed_ai_colours_and_lyrics_never_crash():
    secs = [{"start": 0, "end": 10, "kind": "intro", "energy": 0.2, "lines": []},
            {"start": 10, "end": 30, "kind": "chorus", "energy": 0.9, "lines": [0, 1]}]
    lines = [(10.5, "ride the black road"), (20.0, "ride the black road")]
    for raw in ({"moments": {"line": 1}}, {"moments": 5, "sections": 3}, {"palette": 9},
                {"sections": [{"i": 0, "palette": 7, "energy": "nan", "accent": 3}]},
                {"sections": [{"i": True, "palette": ["#00ff00"]}]}, {"theme": 5}, [], None):
        out, moments, theme = S.apply_colors(secs, lines, raw)
        assert len(out) == 2 and moments == [] and isinstance(theme, str)
        assert out[0]["palette"] == S.DEFAULT_LOOKS["intro"][0] and 0 <= out[0]["energy"] <= 1
    assert S.apply_colors(secs, lines, {"theme": "Riding the black road to hell"})[2] == ""   # borrowed words
    # colours are for lights: a dark hex keeps its hue at full strength; near-black falls back
    dark = S.apply_colors(secs, lines, {"sections": [{"i": 1, "palette": ["#1a0505", "#000000", "#0a1030"],
                                                       "accent": "#010101"}]})[0][1]
    assert dark["palette"] == ["#ff3131", "#3555ff", "#3555ff"] and dark["accent"] == S.DEFAULT_LOOKS["chorus"][1]
    assert all(max(int(c[i:i + 2], 16) for i in (1, 3, 5)) == 255
               for pal, acc in S.DEFAULT_LOOKS.values() for c in pal + [acc])
    assert S.apply_colors(secs, lines, {"theme": "Hellfire on the porch"})[2] == "Hellfire on the porch"
    assert S.parse_lrc({"bad": 1}) == [] and S.parse_lrc(None) == []
    assert S.pick_lyrics([{"duration": 200, "syncedLyrics": {"bad": 1}}], 200) is None
    lrc = "[00:01.00] a\n[00:02.00] b"
    assert S.lyrics_by_id(7, get=lambda i: {"syncedLyrics": lrc}) == [(1.0, "a"), (2.0, "b")]
    assert S.lyrics_by_id(None) == [] and S.lyrics_by_id("7") == []
    assert S.lyrics_by_id(7, get=lambda i: 1 / 0) == []


def test_extend_keeps_the_bar_lines_and_colours_the_new_part_like_the_old(song):
    lines = _lines(song, MADE_UP)
    band = song["beats"][16:]
    for k in (0, 2):                                                            # a hint on beat 1, then on 3
        want = band[k::4]
        m = S.analyze(song["path"], song["end"], downbeat_hint=want[:5])
        assert sum(_near(b, m["downbeats"], 0.05) for b in want) >= 0.9 * len(want)
    lines = _lines(song, [(4, "the bell tolls twelve"), (5, "the bell tolls twelve"), (7, "a lantern on the hill"),
                          (8, "the gate swings wide"), (9, "nobody walks home"), (11, "the bell tolls twelve"),
                          (12, "the bell tolls twelve")])
    old = 20.0                                                                  # cut in the middle of the verse
    m26 = S.analyze(song["path"], old, [t for t, _ in lines if t < old])
    stored, _, _ = S.apply_colors(S.sections_from_lyrics([x for x in lines if x[0] < old], m26, old), lines, None)
    assert [x["kind"] for x in stored][-2:] == ["chorus", "verse"]
    for s in stored:
        if s["kind"] == "chorus":
            s["palette"] = ["#010203", "#040506", "#070809"]
    stored = [{k: s[k] for k in ("start", "end", "kind", "palette", "accent", "energy")} for s in stored]
    full = S.analyze(song["path"], song["end"], downbeat_hint=m26["downbeats"])
    secs = S.extend_sections(stored, full, lines, old, song["end"])
    assert secs[:len(stored) - 1] == stored[:-1] and secs[len(stored) - 1]["start"] == stored[-1]["start"]
    assert all(a["end"] == b["start"] for a, b in zip(secs, secs[1:]))
    assert secs[-1]["end"] == pytest.approx(song["end"], abs=0.01)
    new = secs[len(stored):]
    assert any(s["kind"] == "chorus" for s in new)                              # the words come back round
    assert all(s["palette"] == ["#010203", "#040506", "#070809"] for s in new if s["kind"] == "chorus")


def test_extend_keeps_clear_air_after_the_fog_already_there():
    import haunt_director.api.app as A
    fog = lambda i, t: {"id": i, "t": t, "type": "fog", "duration_s": 3.0, "params": {"intensity": 0.7}}
    data = {"tracks": {"fog_main": [fog("a", 20.0)],
                       "light_skelly": [{"id": "x", "t": 1.0, "type": "blackout", "duration_s": 0, "params": {}},
                                        {"id": "end1", "t": 25.0, "type": "blackout", "duration_s": 0, "params": {}}]}}
    n = A._merge_extension(data, {"fog_main": [fog("n1", 28.0), fog("n2", 40.0)]}, {"end1"},
                           {"fog_main": "dmx_fog", "light_skelly": "dmx_fixture"})
    assert [e["id"] for e in data["tracks"]["fog_main"]] == ["a", "n2"] and n == 1
    assert [e["id"] for e in data["tracks"]["light_skelly"]] == ["x"]


def test_light_timing_never_cuts_a_scene_short(monkeypatch):
    A = _boot(monkeypatch)
    import time as _t
    from haunt_director.domain.scenes import Scene, Event
    red = {"color": "#ff0000", "brightness": 1.0}
    with TestClient(A.app) as c:
        before = c.get("/api/output").json()["light_delay_ms"]
        p, bus = A.app.state.player, A.app.state.bus
        try:
            # lights 300 ms late: the blackout at 0.85 s still happens, and is what's held
            c.post("/api/output", json={"light_delay_ms": 300})
            sc = Scene(id="lag-end", name="x", duration_s=1.0, tracks={"light_skelly": [
                Event(id="a", t=0, type="set_color", params=red), Event(id="b", t=0.85, type="blackout"),
                Event(id="c", t=0.9, type="set_color", params=red), Event(id="d", t=1.0, type="blackout")]})
            c.portal.call(p.play, sc)
            t0 = p._run.start
            assert bus.compose(t0 + 1.25)[1] == 255                            # still red after the sound ends
            _t.sleep(1.6)
            assert not p.is_playing
            assert bus.compose(_t.monotonic())[1] == 0                         # the held frame is the dark one
            # lights 300 ms early: the sound waits instead, so the first flash isn't skipped
            c.post("/api/output", json={"light_delay_ms": -300})
            sc = Scene(id="lag-start", name="x", duration_s=1.0, tracks={"light_skelly": [
                Event(id="a", t=0.15, type="set_color", params=red),
                Event(id="b", t=0.25, type="set_brightness", params={"brightness": 0.0})]})
            c.portal.call(p.play, sc)
            run = p._run
            assert run.audio_lag == pytest.approx(0.3)
            assert bus.compose(run.start + 0.2)[1] == 255                      # the flash at 0.15-0.25 s shows
            c.portal.call(p.stop)
        finally:
            c.post("/api/output", json={"light_delay_ms": before})
