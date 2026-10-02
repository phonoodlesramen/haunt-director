"""First-run seeding, FPP provisioning that never clobbers other overlay models,
same-origin protection for writes, and the output frame matching the channel block."""
import json

from fastapi.testclient import TestClient

from haunt_director.fpp.provision import merged_models
from haunt_director import store as ST


def test_overlay_merge_keeps_other_models_and_is_idempotent():
    mine = merged_models(None, "haunt", 1, 64)
    assert merged_models(mine, "haunt", 1, 64) is None                  # already right: no fppd restart
    others = json.dumps({"autoCreate": True, "models": [
        {"Name": "Matrix", "Type": "Channel", "StartChannel": 1000, "ChannelCount": 300}]})
    m = json.loads(merged_models(others, "haunt", 1, 64))
    assert [x["Name"] for x in m["models"]] == ["Matrix", "haunt"] and m["autoCreate"] is True
    resized = json.loads(merged_models(mine, "haunt", 1, 128))
    assert len(resized["models"]) == 1 and resized["models"][0]["ChannelCount"] == 128
    extra = json.loads(mine); extra["models"][0]["SomethingFppAdded"] = 1
    assert merged_models(json.dumps(extra), "haunt", 1, 64) is None     # FPP's extra keys don't count as drift


def test_first_run_seeds_data_from_examples(tmp_path, monkeypatch):
    ex = tmp_path / "data-example"; (ex / "profiles").mkdir(parents=True)
    (ex / "devices.json").write_text("[]")
    monkeypatch.setattr(ST, "EXAMPLE_DIR", ex)
    d = tmp_path / "data"
    ST.ensure_data_dir(d)
    assert (d / "devices.json").read_text() == "[]"
    (d / "devices.json").write_text('[{"edited": true}]')
    ST.ensure_data_dir(d)                                               # never overwrites your show
    assert "edited" in (d / "devices.json").read_text()


def test_next_halloween_rolls_over():
    from haunt_director.show import _next_halloween
    y, m, dd = _next_halloween().split("-")
    assert (m, dd) == ("10", "31") and int(y) >= 2026


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


def test_cross_site_writes_are_refused_same_site_and_scripts_are_not(monkeypatch):
    A = _boot(monkeypatch)
    with TestClient(A.app) as c:
        evil = {"Origin": "http://evil.example"}
        assert c.post("/api/stop", headers=evil).status_code == 403
        assert c.post("/api/show/trigger", headers=evil).status_code == 403
        assert c.post("/api/stop", headers={"Origin": "null"}).status_code == 403
        assert c.post("/api/stop", headers={"Origin": "http://testserver"}).status_code == 200   # the app itself
        assert c.post("/api/stop").status_code == 200                                          # curl / scripts
        assert c.get("/api/health", headers=evil).status_code == 200                           # reads unaffected


def test_output_frame_spans_the_configured_block(monkeypatch):
    A = _boot(monkeypatch)
    with TestClient(A.app):
        assert A.app.state.bus.nch == A.settings.dmx_end_channel == 64
