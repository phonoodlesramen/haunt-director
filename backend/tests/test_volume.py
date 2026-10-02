"""App master volume: a perceptual slider, applied live to everything the Pi is
playing, without changing any scene's internal balance, and persisted."""
import time

import pytest
from fastapi.testclient import TestClient

from haunt_director.audio import AudioEngine
from haunt_director.player import ShowPlayer


class Ch:
    def __init__(self, snd=None): self.vol, self.snd = None, snd
    def set_volume(self, v): self.vol = v
    def get_sound(self): return self.snd


def _engine(tmp_path):
    eng = AudioEngine(tmp_path)          # pygame absent here -> starts unavailable
    eng._ok = True                       # drive the mixer bookkeeping directly
    snd = object()
    eng._music = (Ch(), object(), 0.8)
    eng._vo, eng._vo_gain = Ch(), 0.5
    eng._cue = {"wind": (Ch(snd), snd, 0.6)}
    return eng


def test_master_scales_every_live_channel(tmp_path):
    eng = _engine(tmp_path)
    eng.set_master(0.25)
    assert eng._music[0].vol == pytest.approx(0.8 * 0.25)
    assert eng._vo.vol == pytest.approx(0.5 * 0.25)
    assert eng._cue["wind"][0].vol == pytest.approx(0.6 * 0.25)


def test_master_respects_lane_mute(tmp_path):
    eng = _engine(tmp_path)
    eng.set_mute({"music"})
    eng.set_master(0.5)
    assert eng._music[0].vol == 0.0                    # muted lane stays silent
    assert eng._vo.vol == pytest.approx(0.25)


def test_slider_curve_and_clamp():
    class A:
        def __init__(self): self.m = None
        def set_master(self, g): self.m = g
    p = ShowPlayer(ctrl=None, bus=type("B", (), {"fog_enabled": True})(), audio=A())
    for pct, gain in ((100, 1.0), (75, 0.5625), (50, 0.25), (0, 0.0)):
        p.set_volume(pct)
        assert p.audio.m == pytest.approx(gain)
    p.set_volume(250)
    assert p.volume == 100 and p.audio.m == 1.0


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


def test_volume_api_persists_and_validates(monkeypatch):
    app = _boot(monkeypatch)
    with TestClient(app) as c:
        assert c.post("/api/output", json={"volume": 40}).json()["volume"] == 40
        assert c.get("/api/output").json()["volume"] == 40
        assert c.post("/api/output", json={"volume": 150}).status_code == 422
        assert c.post("/api/output/test-sound").status_code == 503   # no Pi audio in tests
    with TestClient(app) as c:                                       # restart: restored
        assert c.get("/api/output").json()["volume"] == 40
        c.post("/api/output", json={"volume": 100})


def test_test_sound_picks_setting_then_bundled_then_any_sfx(tmp_path, monkeypatch):
    import haunt_director.api.app as A
    assert A._test_sound_file(tmp_path) is None
    (tmp_path / "sfx-zz-scream.mp3").write_bytes(b"x")
    assert A._test_sound_file(tmp_path) == "sfx-zz-scream.mp3"
    (tmp_path / "sfx-stinger-cc0.mp3").write_bytes(b"x")
    assert A._test_sound_file(tmp_path) == "sfx-stinger-cc0.mp3"
    (tmp_path / "my-laugh.mp3").write_bytes(b"x")
    monkeypatch.setattr(A.settings, "test_sound", "my-laugh.mp3")
    assert A._test_sound_file(tmp_path) == "my-laugh.mp3"
