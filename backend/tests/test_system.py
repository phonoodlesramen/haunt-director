"""FPP Health: the setup checker reads real FPP shapes correctly, suggests the
right fix, never crashes off-Pi, and the API refuses risky actions unless
they're confirmed (and, for disruptive fixes, unless the show is idle)."""
import json

import pytest
from fastapi.testclient import TestClient

from haunt_director import system as S
from haunt_director.config import Settings


FPPD_OK = {"version": "9.5", "status_name": "idle", "mode_name": "player", "uptimeTotalSeconds": 600,
           "channelInputsEnabled": True, "channelOutputsEnabled": True, "powerBad": False}
CO_OTHER = {"channelOutputs": [{"enabled": 1, "type": "DMX-Open", "startChannel": 1,
                                "channelCount": 512, "device": "ttyUSB0"}], "status": "OK"}
CARDS = """ 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 2 [system         ]: USB-Audio - iMic USB audio system
                      Griffin Technology, Inc iMic USB audio system at usb-xhci-hcd.0-1, full speed
"""


def _rig(monkeypatch, *, apache=200, php=200, inputs=True, models=("haunt",), e131_ours_on=False,
         restart="on-failure", dev_present=True):
    fppd = dict(FPPD_OK, channelInputsEnabled=inputs)

    def http(url, timeout=3.0):
        if url.endswith(":32322/fppd/status"):
            return 200, fppd
        if url.endswith("/api/fppd/status"):
            return apache, None
        if url.endswith("/api/system/status"):
            return php, {}
        if "/api/channel/output/co-other" in url:
            return 200, CO_OTHER
        if "/api/models" in url:
            return 200, list(models)
        return 0, None

    def run(cmd, timeout=8.0, input_text=None):
        if cmd[:2] == ["systemctl", "list-unit-files"]:
            return 0, "php8.2-fpm.service enabled enabled\n"
        if cmd[:2] == ["systemctl", "show"]:
            return 0, restart + "\n"
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, "active\n"
        if cmd[:3] == ["sudo", "-n", "cat"]:
            cu = {"channelOutputs": [{"type": "universes", "enabled": 1 if e131_ours_on else 0,
                                      "universes": [{"description": "Haunt Director", "id": 1}]}]}
            return 0, json.dumps(cu)
        return 0, ""

    monkeypatch.setattr(S, "_http", http)
    monkeypatch.setattr(S, "_run", run)
    monkeypatch.setattr(S, "_read", lambda p: CARDS if p == "/proc/asound/cards" else None)
    real_exists = S.Path.exists
    monkeypatch.setattr(S.Path, "exists", lambda self: True if str(self) in ("/opt/fpp", "/dev/ttyUSB0") and dev_present
                        else (False if str(self) == "/dev/ttyUSB0" else real_exists(self)))


def _ctx(transport="ddp", audio_ok=True, gpio=4, vit=None):
    s = Settings(output_transport=transport)
    return {"settings": s, "vitals": vit or {"clock_synced": True, "throttled": {"raw": "0x0", "flags": []}, "temp_c": 48.0},
            "bus_stats": {"ready": True, "fps": 25.0, "target_fps": 25, "transport": transport, "send_errors": 0},
            "audio_state": {"ok": audio_ok, "device": "iMic USB audio system, USB Audio"},
            "show_status": {"gpio_pin": gpio, "gpio_ok": gpio is not None, "presence_active": False}}


def _by_id(checks):
    return {c["id"]: c for c in checks}


def test_healthy_rig_is_all_green(monkeypatch):
    _rig(monkeypatch)
    ctx = _ctx(); ctx["fpp"] = S.fpp_status()
    c = _by_id(S.run_checks(ctx))
    bad = {k: v["status"] for k, v in c.items() if v["status"] != "ok"}
    assert bad == {}
    assert "64-channel" in c["dmx_out"]["detail"]        # gentle tip: 512 ch when fixtures stop at 64


def test_stuck_apache_offers_a_safe_restart(monkeypatch):
    _rig(monkeypatch, apache=0, php=503)
    ctx = _ctx(); ctx["fpp"] = S.fpp_status()
    c = _by_id(S.run_checks(ctx))["fpp_web"]
    assert c["status"] == "fail" and "Apache and PHP" in c["detail"]
    assert c["fix"] == {"label": "Restart FPP web services", "disruptive": False}


def test_missing_boot_retry_ddp_input_and_leftover_e131(monkeypatch):
    _rig(monkeypatch, restart="no", inputs=False, e131_ours_on=True)
    ctx = _ctx(); ctx["fpp"] = S.fpp_status()
    c = _by_id(S.run_checks(ctx))
    assert c["boot_retry"]["status"] == "warn" and c["boot_retry"]["fix"]["disruptive"] is False
    assert c["ddp_in"]["status"] == "fail" and c["ddp_in"]["fix"]["label"] == "Turn it on"
    assert c["e131_out"]["status"] == "warn" and c["e131_out"]["fix"]["disruptive"] is True


def test_rest_transport_skips_ddp_checks(monkeypatch):
    _rig(monkeypatch, inputs=False)
    ctx = _ctx(transport="rest"); ctx["fpp"] = S.fpp_status()
    ids = _by_id(S.run_checks(ctx))
    assert "ddp_in" not in ids and "e131_out" not in ids


def test_audio_on_hdmi_points_at_the_usb_adapter(monkeypatch):
    _rig(monkeypatch)
    ctx = _ctx(audio_ok=False); ctx["fpp"] = S.fpp_status()
    c = _by_id(S.run_checks(ctx))["audio"]
    assert c["status"] == "fail" and "iMic USB audio system" in c["fix"]["label"] and c["fix"]["disruptive"]
    assert 'slave.pcm "hw:CARD=system,DEV=0"' in S.asoundrc_for("system")


def test_missing_overlay_dongle_and_power_flags(monkeypatch):
    _rig(monkeypatch, models=(), dev_present=False)
    ctx = _ctx(vit={"throttled": {"raw": "0x50000", "flags": ["under-voltage since boot", "throttled since boot"]}})
    ctx["fpp"] = S.fpp_status()
    c = _by_id(S.run_checks(ctx))
    assert c["overlay"]["status"] == "fail" and c["overlay"]["fix"]["disruptive"]
    assert c["dmx_out"]["status"] == "fail" and "isn't plugged in" in c["dmx_out"]["detail"]
    assert c["power"]["status"] == "warn" and "under-voltage" in c["power"]["detail"]


def test_throttle_bits_decode(monkeypatch):
    monkeypatch.setattr(S, "_run", lambda cmd, timeout=8.0, input_text=None: (0, "throttled=0x50005\n"))
    t = S._throttled()
    assert t["raw"] == "0x50005"
    assert {"under-voltage now", "throttled now", "under-voltage since boot", "throttled since boot"} <= set(t["flags"])


def test_off_pi_everything_is_unknown_not_an_error(monkeypatch):
    monkeypatch.setattr(S, "_http", lambda url, timeout=3.0: (0, None))
    monkeypatch.setattr(S, "_run", lambda cmd, timeout=8.0, input_text=None: (127, ""))
    monkeypatch.setattr(S, "_read", lambda p: None)
    real_exists = S.Path.exists
    monkeypatch.setattr(S.Path, "exists", lambda self: False if str(self) == "/opt/fpp" else real_exists(self))
    ctx = _ctx(); ctx["fpp"] = S.fpp_status()
    c = _by_id(S.run_checks(ctx))
    assert c["fppd"]["status"] == "unknown" and c["fpp_web"]["status"] == "unknown"
    assert all(x["fix"] is None for x in c.values() if x["status"] == "unknown")


def test_ddp_fix_merges_existing_inputs(monkeypatch):
    written = {}
    existing = {"channelInputs": [{"type": "universes", "enabled": 0, "timeout": 2500,
                                   "universes": [{"id": 7, "description": "someone else's E1.31 input"}]}]}
    monkeypatch.setattr(S, "_read_root_json", lambda p: existing)
    monkeypatch.setattr(S, "_write_root", lambda p, t: written.__setitem__(p, json.loads(t)) or True)
    monkeypatch.setattr(S, "_http", lambda url, timeout=3.0: (200, {"channelInputsEnabled": True}))
    ok, _ = S.fix_ddp_input()
    new = written[f"{S.FPP_CFG}/ci-universes.json"]["channelInputs"][0]
    assert ok and new["enabled"] == 1 and new["timeout"] == 2500 and new["universes"][0]["id"] == 7
    assert f"{S.FPP_CFG}/ci-universes.json.haunt-backup" in written


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


def test_system_api_shapes_and_guards(monkeypatch):
    A = _boot(monkeypatch)
    calls = []
    monkeypatch.setitem(A._FIXES, "boot_retry", (False, lambda: (calls.append("boot") or (True, "done"))))
    monkeypatch.setitem(A._FIXES, "fppd", (True, lambda: (calls.append("fppd") or (True, "done"))))
    monkeypatch.setattr(A._sys, "reboot", lambda: (calls.append("reboot") or (True, "rebooting")))
    with TestClient(A.app) as c:
        h = c.get("/api/system/health").json()
        assert {"fpp", "vitals", "services", "output", "audio", "sensor", "show"} <= set(h)
        r = c.get("/api/system/checks").json()
        assert r["checks"] and set(r["counts"]) == {"ok", "warn", "fail", "unknown"}

        assert c.post("/api/system/checks/nope/fix", json={"confirm": True}).status_code == 404
        assert c.post("/api/system/checks/boot_retry/fix", json={}).status_code == 400
        assert c.post("/api/system/checks/boot_retry/fix", json={"confirm": True}).json()["ok"]
        # a bare cross-site style POST (no JSON body) is refused, not executed
        assert c.post("/api/system/checks/boot_retry/fix", content="confirm=true",
                      headers={"Content-Type": "text/plain"}).status_code == 422

        A.app.state.show.armed = True                      # disruptive fixes wait for an idle show
        assert c.post("/api/system/checks/fppd/fix", json={"confirm": True}).status_code == 409
        A.app.state.show.armed = False
        assert c.post("/api/system/checks/fppd/fix", json={"confirm": True}).status_code == 200

        assert c.post("/api/system/action", json={"action": "reboot"}).status_code == 400
        assert c.post("/api/system/action", json={"action": "format_disk", "confirm": True}).status_code == 422
        assert c.post("/api/system/action", json={"action": "reboot", "confirm": True}).json()["ok"]
    assert calls == ["boot", "fppd", "reboot"]


def test_no_wildcard_cors(monkeypatch):
    A = _boot(monkeypatch)
    with TestClient(A.app) as c:
        r = c.options("/api/system/action", headers={"Origin": "http://evil.example",
                                                      "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
