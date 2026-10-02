"""End-to-end through the real FastAPI app (real lifespan, real Store/devices/
scenes, real ShowDirector), with the wire swapped for a recording transport."""
import time

from fastapi.testclient import TestClient


def _boot(monkeypatch):
    import haunt_director.api.app as A
    import haunt_director.fpp.provision as P
    monkeypatch.setattr(P, "ensure_all", lambda *a, **k: {"changed": False})
    sent = []

    class T:
        name = "test"
        def open(self): pass
        def send(self, frame, now): sent.append(frame); return 1
        def close(self, zero_frame=None):
            if zero_frame is not None: sent.append(zero_frame)

    monkeypatch.setattr(A, "make_transport", lambda s: T())
    return A.app, sent


def test_live_card_overrides_then_releases(monkeypatch):
    app, sent = _boot(monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/devices/light_skelly/set", json={"rgb": [255, 0, 0], "brightness": 200})
        assert r.status_code == 200
        assert "light_skelly" in c.get("/api/play/state").json()["overrides"]
        time.sleep(0.15)
        assert sent[-1][0] == 200 and sent[-1][1] == 255          # on the wire via the bus
        assert c.post("/api/devices/light_skelly/release").status_code == 200
        assert c.get("/api/play/state").json()["overrides"] == []
        assert c.post("/api/devices/nope/release").status_code == 404


def test_stop_everything_disarms_the_show_and_goes_dark(monkeypatch):
    app, sent = _boot(monkeypatch)
    with TestClient(app) as c:
        c.post("/api/show/arm", json={"armed": True})
        time.sleep(0.4)
        assert c.get("/api/show").json()["armed"] is True
        assert c.get("/api/play/status").json()["playing"] is True     # ambient running
        r = c.post("/api/stop").json()
        assert r["stopped"] and r["disarmed"]
        time.sleep(0.4)
        assert c.get("/api/show").json()["armed"] is False
        assert c.get("/api/play/status").json()["playing"] is False    # did NOT restart
        assert sent[-1] == bytes(64)
        h = c.get("/api/health").json()
        assert h["output"]["transport"] == "test" and h["output"]["alive"]
    assert sent[-1] == bytes(64)                                          # shutdown leaves it dark


def test_arming_clears_leftover_overrides(monkeypatch):
    app, sent = _boot(monkeypatch)
    with TestClient(app) as c:
        c.post("/api/devices/light_path/set", json={"rgb": [0, 255, 0], "brightness": 255})
        assert c.get("/api/play/state").json()["overrides"] == ["light_path"]
        c.post("/api/show/arm", json={"armed": True})
        assert c.get("/api/play/state").json()["overrides"] == []
        c.post("/api/show/arm", json={"armed": False})


def test_fog_disable_is_instant(monkeypatch):
    app, sent = _boot(monkeypatch)
    with TestClient(app) as c:
        c.post("/api/output", json={"fog_enabled": True})
        assert c.post("/api/devices/fog_main/fog", json={"seconds": 3}).status_code == 200
        time.sleep(0.12)
        assert sent[-1][48] == 255
        c.post("/api/output", json={"fog_enabled": False})
        time.sleep(0.12)
        assert sent[-1][48] == 0
        c.post("/api/output", json={"fog_enabled": True})
