"""Voice ducking: while a voice speaks, the music bed and looping ambience dip by
the set depth and come back after; one-shot SFX and the voice itself never move.
Envelope: linear in dB, fast attack, short hold, gentle release."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from haunt_director import audio as A
from haunt_director.audio import AudioEngine
from haunt_director.player import ShowPlayer, DUCK_DEFAULT_DB
from haunt_director.domain.scenes import Scene, SceneAudio, AudioCue


class Ch:
    def __init__(self, snd=None, busy=False):
        self.vol, self.snd, self.busy = None, snd, busy
    def set_volume(self, v): self.vol = v
    def get_sound(self): return self.snd
    def get_busy(self): return self.busy


def _engine(tmp_path, depth=9):
    eng = AudioEngine(tmp_path)              # no pygame here -> drive the bookkeeping directly
    eng._ok = True
    eng._music = (Ch(), object(), 1.0)
    eng._vo, eng._vo_gain = Ch(busy=False), 1.0
    bed, hit, line = object(), object(), object()
    eng._cue = {"wind": (Ch(bed, busy=True), bed, 0.5),
                "thunder": (Ch(hit, busy=True), hit, 1.0),
                "whisper": (Ch(line, busy=False), line, 1.0)}
    eng._cue_role = {"wind": "bed", "thunder": "sfx", "whisper": "voice"}
    eng.set_duck_db(depth)
    eng.set_master(1.0)
    return eng


def _run(eng, t0, t1, dt=0.025):
    t = t0
    while t <= t1 + 1e-9:
        eng._duck_step(t)
        t += dt
    return t1


def _db(v, ref):
    import math
    return 20 * math.log10(v / ref)


def test_voiceover_ducks_music_and_beds_only(tmp_path):
    eng = _engine(tmp_path)
    _run(eng, 0.0, 0.5)                                   # quiet: nothing moves
    assert eng._music[0].vol == pytest.approx(1.0)
    eng._vo.busy = True                                   # voice-over starts
    _run(eng, 0.5, 0.8)                                   # 0.3 s: past the 0.15 s attack
    assert _db(eng._music[0].vol, 1.0) == pytest.approx(-9, abs=0.01)
    assert _db(eng._cue["wind"][0].vol, 0.5) == pytest.approx(-9, abs=0.01)
    assert eng._cue["thunder"][0].vol == pytest.approx(1.0)      # one-shot hit untouched
    assert eng._vo.vol == pytest.approx(1.0)                     # the voice itself untouched


def test_attack_is_fast_and_release_is_gentle_with_hold(tmp_path):
    eng = _engine(tmp_path)
    eng._vo.busy = True
    _run(eng, 0.0, 0.1)
    assert -9 < eng._duck_db < -3                         # mid-attack after 0.1 s
    _run(eng, 0.1, 1.0)
    assert eng._duck_db == pytest.approx(-9)
    eng._vo.busy = False                                  # line ends at t=1.0
    _run(eng, 1.0, 1.35)
    assert eng._duck_db == pytest.approx(-9)              # held through a short gap
    _run(eng, 1.35, 1.8)
    assert -9 < eng._duck_db < 0                          # easing back up
    _run(eng, 1.8, 2.5)
    assert eng._duck_db == 0.0 and eng._music[0].vol == pytest.approx(1.0)


def test_voice_layer_ducks_too(tmp_path):
    eng = _engine(tmp_path)
    eng._cue["whisper"][0].busy = True                    # a layer the library calls a voice
    _run(eng, 0.0, 0.5)
    assert eng._duck_db == pytest.approx(-9)


def test_muted_voice_or_zero_depth_never_ducks(tmp_path):
    eng = _engine(tmp_path)
    eng._vo.busy = True
    eng.set_mute({"voiceover"})
    _run(eng, 0.0, 0.5)
    assert eng._duck_db == 0.0
    eng2 = _engine(tmp_path, depth=0)
    eng2._vo.busy = True
    _run(eng2, 0.0, 0.5)
    assert eng2._duck_db == 0.0 and eng2._music[0].vol == pytest.approx(1.0)


def test_master_and_mute_still_compose_with_duck(tmp_path):
    eng = _engine(tmp_path)
    eng._vo.busy = True
    _run(eng, 0.0, 0.5)
    eng.set_master(0.25)
    assert eng._music[0].vol == pytest.approx(0.25 * 10 ** (-9 / 20))
    eng.set_mute({"music"})
    assert eng._music[0].vol == 0.0


def test_stop_resets_the_envelope(tmp_path):
    eng = _engine(tmp_path)
    eng._vo.busy = True
    _run(eng, 0.0, 0.5)

    class PG:
        class mixer:
            @staticmethod
            def stop(): pass
            class music:
                @staticmethod
                def stop(): pass
    eng._pg = PG
    eng.stop()
    assert eng._duck_db == 0.0 and eng._cue_role == {}


def test_roles_from_library_and_cue_shape():
    p = ShowPlayer(ctrl=None, bus=type("B", (), {"fog_enabled": True})(), audio=None)
    p.voice_files = lambda files: {f for f in files if f.startswith("shadow")}
    sc = Scene(id="s", name="s", audio=SceneAudio(music="m.wav", layers=[
        AudioCue(id="a", file="amb-wind.wav", loop=True),
        AudioCue(id="b", file="sfx-scream.mp3", loop=False),
        AudioCue(id="c", file="shadow-1.mp3", loop=False)]))
    assert p._layer_roles(sc) == {"a": "bed", "b": "sfx", "c": "voice"}
    p.voice_files = None                                  # no classifier: loops are beds
    assert p._layer_roles(sc)["c"] == "sfx"


def test_player_passes_roles_to_the_engine():
    class Rec:
        def __init__(self): self.cues = []
        def preload(self, files, pin=False): pass
        def play_cue(self, f, gain=1.0, loop=False, key=None, role=None): self.cues.append((key, role))
        def __getattr__(self, n): return lambda *a, **k: None

    from haunt_director.output import OutputBus
    from haunt_director.store import Store

    class T:
        name = "t"
        def open(self): pass
        def send(self, f, now): return 1
        def close(self, z=None): pass

    store = Store()
    rec = Rec()
    p = ShowPlayer(type("C", (), {"store": store})(), OutputBus(store, T()), audio=rec)
    p.voice_files = lambda files: {f for f in files if f.startswith("shadow")}
    sc = Scene(id="s", name="s", duration_s=0.3, audio=SceneAudio(layers=[
        AudioCue(id="bed", file="amb-wind.wav", loop=True),
        AudioCue(id="line", file="shadow-1.mp3", at_s=0.05)]))

    async def go():
        await p.play(sc)
        await asyncio.sleep(0.15)
        await p.stop()
    asyncio.run(go())
    assert ("bed", "bed") in rec.cues and ("line", "voice") in rec.cues


def test_set_duck_clamps_and_reaches_engine():
    class E:
        def __init__(self): self.d = None
        def set_duck_db(self, d): self.d = d
    p = ShowPlayer(ctrl=None, bus=type("B", (), {"fog_enabled": True})(), audio=E())
    assert p.duck_db == DUCK_DEFAULT_DB == 9
    p.set_duck(12); assert p.audio.d == 12
    p.set_duck(99); assert p.duck_db == 18
    p.set_duck(-3); assert p.duck_db == 0


def test_voice_files_follow_library_categories(monkeypatch):
    import haunt_director.api.app as APP
    monkeypatch.setattr(APP, "_load_audio_lib", lambda: {"spooky-poem.mp3": {"category": "voice"},
                                                         "vo-07.mp3": {"category": "sfx"}})
    got = APP._voice_files(["spooky-poem.mp3", "vo-07.mp3", "shadow-2.mp3", "amb-wind.wav", None])
    assert got == {"spooky-poem.mp3", "shadow-2.mp3"}     # library wins; else the name rule


def _boot(monkeypatch):
    import haunt_director.api.app as APP
    import haunt_director.fpp.provision as P
    monkeypatch.setattr(P, "ensure_all", lambda *a, **k: {"changed": False})

    class T:
        name = "test"
        def open(self): pass
        def send(self, f, now): return 1
        def close(self, zero_frame=None): pass
    monkeypatch.setattr(APP, "make_transport", lambda s: T())
    return APP.app


def test_duck_api_persists_and_validates(monkeypatch):
    app = _boot(monkeypatch)
    with TestClient(app) as c:
        assert c.post("/api/output", json={"duck_db": 12}).json()["duck_db"] == 12
        assert c.post("/api/output", json={"duck_db": 19}).status_code == 422
        assert c.get("/api/output").json()["duck_db"] == 12
    with TestClient(app) as c:                                       # restart: restored
        assert c.get("/api/output").json()["duck_db"] == 12
        c.post("/api/output", json={"duck_db": DUCK_DEFAULT_DB})
