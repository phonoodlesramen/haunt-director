"""FPP Health: is everything on the Pi actually up, and is FPP set up the way
Haunt Director needs it?

Three parts, all best-effort (on a dev machine with no FPP every check simply
comes back "unknown" instead of failing):

  * health()   -- live status: FPP daemon, FPP's web UI/API, Pi vitals.
  * checks()   -- a setup checklist for the handful of FPP settings Haunt
                  Director depends on. Each item can carry a one-click fix.
  * actions    -- restart FPP's web services, restart FPP, restart Haunt
                  Director, reboot the Pi.

It deliberately does NOT try to be a second FPP settings page. Anything not on
this list stays in FPP's own UI, which the panel links to.

Everything that blocks (subprocess, HTTP to FPP) is plain sync code; the API
layer runs it in a worker thread. Root work goes through `sudo -n` (never
prompts) -- see deploy/sudoers.d in the repo for exactly what that needs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

FPP_CFG = "/home/fpp/media/config"
DIRECT_FPPD = "http://127.0.0.1:32322"
DROPIN_NAME = "haunt-resilience.conf"
DROPIN = ("[Unit]\nWants=network-online.target\nAfter=network-online.target\n\n"
          "[Service]\nRestart=on-failure\nRestartSec=5\n")
OUR_E131_DESC = "Haunt Director"      # the E1.31 output older versions provisioned


# ---------------------------------------------------------------- primitives
def _run(cmd, timeout=8.0, input_text=None):
    """(returncode, stdout+stderr). Never raises; 127 if the tool is missing."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, ""
    except Exception as e:                       # timeout, permissions, ...
        return 1, str(e)


def _sudo(*args, timeout=30.0, input_text=None):
    return _run(["sudo", "-n", *args], timeout=timeout, input_text=input_text)


def _http(url, timeout=3.0):
    """(status, parsed-json-or-None). status 0 = unreachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def _detached(shell_cmd):
    """Run a command after we've answered the request (it may restart us)."""
    try:
        subprocess.Popen(["sh", "-c", shell_cmd], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except Exception:
        return False


def _read(path):
    try:
        return Path(path).read_text()
    except Exception:
        return None


def _read_root_json(path):
    rc, out = _sudo("cat", path, timeout=6)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def _write_root(path, text):
    rc, _ = _sudo("tee", path, input_text=text, timeout=10)
    if rc == 0:
        _sudo("chown", "fpp:fpp", path, timeout=6)
    return rc == 0


def php_fpm_unit():
    rc, out = _run(["systemctl", "list-unit-files", "--no-legend", "php*-fpm.service"], timeout=5)
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.endswith("-fpm.service"):
            return name
    return None


def service_state(unit):
    rc, out = _run(["systemctl", "is-active", unit], timeout=5)
    s = out.strip().splitlines()[0] if out.strip() else ""
    return s or ("unknown" if rc == 127 else "inactive")


# ---------------------------------------------------------------- vitals
_THROTTLE_BITS = {0: "under-voltage now", 1: "CPU capped now", 2: "throttled now",
                  3: "too hot now", 16: "under-voltage since boot", 17: "CPU capped since boot",
                  18: "throttled since boot", 19: "too hot since boot"}


def _throttled():
    rc, out = _run(["vcgencmd", "get_throttled"], timeout=4)
    m = re.search(r"0x([0-9a-fA-F]+)", out or "")
    if rc != 0 or not m:
        return None
    v = int(m.group(1), 16)
    return {"raw": hex(v), "flags": [lbl for bit, lbl in _THROTTLE_BITS.items() if v & (1 << bit)]}


def vitals():
    out = {}
    up = _read("/proc/uptime")
    if up:
        out["uptime_s"] = int(float(up.split()[0]))
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    if t and t.strip().lstrip("-").isdigit():
        out["temp_c"] = round(int(t) / 1000.0, 1)
    out["throttled"] = _throttled()
    la = _read("/proc/loadavg")
    if la:
        out["load_1m"] = float(la.split()[0])
    mi = _read("/proc/meminfo")
    if mi:
        kv = {l.split(":")[0]: int(l.split()[1]) for l in mi.splitlines() if l.split()[1:2] and l.split()[1].isdigit()}
        if kv.get("MemTotal"):
            out["mem_used_pct"] = round(100 * (1 - kv.get("MemAvailable", 0) / kv["MemTotal"]))
    try:
        du = shutil.disk_usage("/")
        out["disk_free_gb"] = round(du.free / 1e9, 1)
    except Exception:
        pass
    w = _read("/proc/net/wireless")
    if w:
        for line in w.splitlines()[2:]:
            parts = line.split()
            if len(parts) > 3 and parts[0].endswith(":"):
                try:
                    out["wifi"] = {"iface": parts[0][:-1], "signal_dbm": int(float(parts[3]))}
                except Exception:
                    pass
    rc, ips = _run(["hostname", "-I"], timeout=3)
    if rc == 0:
        out["ips"] = [ip for ip in ips.split() if ":" not in ip]
    rc, hn = _run(["hostname"], timeout=3)
    if rc == 0:
        out["hostname"] = hn.strip()
    rc, ntp = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=4)
    if rc == 0 and ntp.strip() in ("yes", "no"):
        out["clock_synced"] = ntp.strip() == "yes"
    return out


def fpp_status(base_url="http://127.0.0.1"):
    """What FPP looks like right now, from three angles: the daemon directly,
    through Apache (what the app and FPP's UI use), and FPP's PHP layer."""
    d_code, d = _http(DIRECT_FPPD + "/fppd/status")
    a_code, _ = _http(base_url.rstrip("/") + "/api/fppd/status")
    p_code, p = _http(base_url.rstrip("/") + "/api/system/status")
    d = d or {}
    return {
        "daemon_ok": d_code == 200,
        "web_ok": a_code == 200,
        "php_ok": p_code == 200,
        "http": {"daemon": d_code, "apache": a_code, "php": p_code},
        "version": d.get("version"),
        "status": d.get("status_name"),
        "mode": d.get("mode_name"),
        "uptime_s": d.get("uptimeTotalSeconds"),
        "power_bad": d.get("powerBad"),
        "inputs_enabled": d.get("channelInputsEnabled"),
        "outputs_enabled": d.get("channelOutputsEnabled"),
        "warnings": d.get("warnings") or (p or {}).get("warnings") or [],
    }


def services():
    php = php_fpm_unit()
    units = ["fppd", "apache2"] + ([php.removesuffix(".service")] if php else [])
    return {u: service_state(u) for u in units}


# ---------------------------------------------------------------- checks
def _c(cid, title, status, detail, fix=None, help=None):
    return {"id": cid, "title": title, "status": status, "detail": detail,
            "fix": fix, "help": help}


def _fix(label, disruptive=False):
    return {"label": label, "disruptive": bool(disruptive)}


def _usb_audio_cards():
    """[(index, card_id, description)] for USB audio adapters."""
    txt = _read("/proc/asound/cards") or ""
    cards = []
    for m in re.finditer(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*USB-Audio\s*-\s*(.+)$", txt, re.M):
        cards.append((int(m.group(1)), m.group(2), m.group(3).strip()))
    return cards


def run_checks(ctx):
    """ctx: dict with settings, bus_stats, audio_state, show_status, fpp (fpp_status())."""
    s = ctx["settings"]
    fpp = ctx.get("fpp") or fpp_status(s.fpp_base_url)
    on_pi = fpp["http"]["daemon"] != 0 or Path("/opt/fpp").exists()
    out = []
    rig = ctx.get("rig") or {}
    block_end = int(rig.get("block_end") or s.dmx_end_channel)
    needed = int(rig.get("needed_end") or 0)

    # 0. The rig itself (System -> Your rig)
    if rig:
        iss = rig.get("issues") or []
        errs = [i for i in iss if i["level"] == "error"]
        n_l, n_f = rig.get("lights", 0), rig.get("foggers", 0)
        what = f"{n_l} light{'s' if n_l != 1 else ''}" + (f" and {n_f} fogger{'s' if n_f != 1 else ''}" if n_f else "")
        more = f" (and {len(iss) - 1} more)" if len(iss) > 1 else ""
        if errs:
            out.append(_c("rig", "Your rig", "fail", errs[0]["message"] + more,
                          help="Fix it under Your rig, above."))
        elif iss:
            out.append(_c("rig", "Your rig", "warn", iss[0]["message"] + more,
                          help="Check it under Your rig, above."))
        elif n_l or n_f:
            out.append(_c("rig", "Your rig", "ok",
                          f"{what}, using DMX channels 1 to {needed}." if needed else f"{what}."))
        else:
            out.append(_c("rig", "Your rig", "warn", "No lights or foggers yet.",
                          help="Add them under Your rig, above."))

    # 1. FPP daemon
    if fpp["daemon_ok"]:
        out.append(_c("fppd", "FPP is running", "ok",
                      f"FPP {fpp.get('version') or '?'} · {fpp.get('mode') or '?'} mode · {fpp.get('status') or 'idle'}"))
    elif on_pi:
        out.append(_c("fppd", "FPP is running", "fail", "The FPP daemon isn't answering.",
                      _fix("Restart FPP", disruptive=True)))
    else:
        out.append(_c("fppd", "FPP is running", "unknown", "No FPP found on this machine."))

    # 2. FPP web UI / API (Apache + PHP). The lights don't need it; the app's FPP checks and FPP's UI do.
    if fpp["web_ok"] and fpp["php_ok"]:
        out.append(_c("fpp_web", "FPP web UI and API", "ok", "Apache and PHP are answering."))
    elif fpp["daemon_ok"] or on_pi:
        bits = [n for n, ok in (("Apache", fpp["web_ok"]), ("PHP", fpp["php_ok"])) if not ok]
        out.append(_c("fpp_web", "FPP web UI and API", "fail",
                      f"{' and '.join(bits)} not answering (a slow boot can leave them stuck).",
                      _fix("Restart FPP web services")))
    else:
        out.append(_c("fpp_web", "FPP web UI and API", "unknown", "No FPP found on this machine."))

    # 3. Boot resilience: Apache/PHP retry on their own if a slow boot times them out
    php = php_fpm_unit()
    units = ["apache2"] + ([php.removesuffix(".service")] if php else [])
    missing = []
    for u in units:
        rc, r = _run(["systemctl", "show", u, "-p", "Restart", "--value"], timeout=4)
        if rc == 127:
            missing = None
            break
        if r.strip() not in ("on-failure", "always"):
            missing.append(u)
    if missing is None or not on_pi:
        out.append(_c("boot_retry", "Web services retry after a slow boot", "unknown", "systemd not available here."))
    elif missing:
        out.append(_c("boot_retry", "Web services retry after a slow boot", "warn",
                      f"{', '.join(missing)} give up for good if boot is slow (common on weak Wi-Fi).",
                      _fix("Add auto-retry"),
                      help="Adds a tiny systemd override: wait for the network, retry every 5 s on failure."))
    else:
        out.append(_c("boot_retry", "Web services retry after a slow boot", "ok", "Apache and PHP auto-retry."))

    # 4. DMX output
    code, co = _http(s.fpp_base_url.rstrip("/") + "/api/channel/output/co-other")
    outs = [o for o in ((co or {}).get("channelOutputs") or []) if o.get("enabled")]
    if code == 0 and not fpp["daemon_ok"]:
        out.append(_c("dmx_out", "DMX output", "unknown", "Can't read FPP's output settings."))
    elif not outs:
        out.append(_c("dmx_out", "DMX output", "fail",
                      "FPP has no enabled DMX/serial output, so nothing reaches the fixtures.",
                      help="Set one up in FPP: Input/Output Setup → Other → add DMX-Open (or DMX-Pro) on your USB dongle."))
    else:
        o = outs[0]
        dev = o.get("device") or ""
        present = bool(dev) and Path("/dev/" + dev).exists()
        tip = ""
        o_start = int(o.get("startChannel") or 1)
        o_end = o_start + int(o.get("channelCount") or 0) - 1
        if int(o.get("channelCount") or 0) > max(64, block_end):
            tip = (f" Tip: your fixtures stop at channel {block_end}; a {max(64, block_end)}-channel "
                   f"output makes each DMX frame much shorter.")
        if dev and present and needed and (o_start > 1 or o_end < needed):
            out.append(_c("dmx_out", "DMX output", "warn",
                          f"FPP sends channels {o_start} to {o_end}, but your rig uses channels up to {needed}. "
                          f"Lights past channel {o_end} stay dark.",
                          help=f"In FPP: Input/Output Setup → Other, set this output to start at 1 with a channel "
                               f"count of {needed} or more (rounding up to {min(512, ((needed + 15) // 16) * 16)} leaves room), "
                               f"save, then restart FPP."))
        elif dev and not present:
            out.append(_c("dmx_out", "DMX output", "fail",
                          f"{o.get('type')} is set to {dev}, but that device isn't plugged in.",
                          help="Check the USB DMX dongle. If it moved (ttyUSB1?), update it in FPP: Input/Output Setup → Other."))
        else:
            out.append(_c("dmx_out", "DMX output", "ok",
                          f"{o.get('type')} on {dev or '?'}, channels {o.get('startChannel')}–"
                          f"{int(o.get('startChannel') or 1) + int(o.get('channelCount') or 0) - 1}.{tip}"))

    # 5. Overlay model (Haunt Director's claim on the DMX channels)
    code, models = _http(s.fpp_base_url.rstrip("/") + "/api/models?simple=true")
    if code == 200 and isinstance(models, list):
        mcount = None
        mf = _read_root_json(f"{FPP_CFG}/model-overlays.json") if s.overlay_model_name in models else None
        for m in ((mf or {}).get("models") or []):
            if isinstance(m, dict) and m.get("Name") == s.overlay_model_name:
                mcount = int(m.get("ChannelCount") or 0)
        transport = str(getattr(s, "output_transport", "rest")).lower()
        if s.overlay_model_name in models and mcount is not None and transport != "ddp" \
                and s.dmx_start_channel + mcount - 1 < block_end:
            out.append(_c("overlay", "Haunt Director's channel block in FPP", "warn",
                          f"Overlay model '{s.overlay_model_name}' covers channels {s.dmx_start_channel} to "
                          f"{s.dmx_start_channel + mcount - 1}, but your rig needs 1 to {block_end}.",
                          _fix("Resize it (restarts FPP)", disruptive=True)))
        elif s.overlay_model_name in models:
            out.append(_c("overlay", "Haunt Director's channel block in FPP", "ok",
                          f"Overlay model '{s.overlay_model_name}' covers channels {s.dmx_start_channel} to "
                          f"{s.dmx_start_channel + mcount - 1 if mcount else block_end}."))
        else:
            out.append(_c("overlay", "Haunt Director's channel block in FPP", "fail",
                          f"Overlay model '{s.overlay_model_name}' is missing.",
                          _fix("Create it (restarts FPP)", disruptive=True)))
    else:
        out.append(_c("overlay", "Haunt Director's channel block in FPP", "unknown", "Can't read FPP's models."))

    # 6/7. Transport plumbing
    transport = str(getattr(s, "output_transport", "rest")).lower()
    if transport == "ddp":
        if fpp.get("inputs_enabled") is True:
            out.append(_c("ddp_in", "FPP accepts Haunt Director's light data (DDP)", "ok", "Network input is on."))
        elif fpp["daemon_ok"]:
            out.append(_c("ddp_in", "FPP accepts Haunt Director's light data (DDP)", "fail",
                          "FPP's network input is off, so it ignores the light frames.",
                          _fix("Turn it on")))
        else:
            out.append(_c("ddp_in", "FPP accepts Haunt Director's light data (DDP)", "unknown", "FPP isn't answering."))
        cu = _read_root_json(f"{FPP_CFG}/co-universes.json")
        ours_on = [o for o in ((cu or {}).get("channelOutputs") or [])
                   if o.get("enabled") and o.get("universes")
                   and all(u.get("description") == OUR_E131_DESC for u in o["universes"])]
        if cu is None:
            out.append(_c("e131_out", "No leftover E1.31 broadcast", "unknown", "Can't read FPP's E1.31 settings."))
        elif ours_on:
            out.append(_c("e131_out", "No leftover E1.31 broadcast", "warn",
                          "An E1.31 output from an older Haunt Director setup is still multicasting over your network 20+ times a second.",
                          _fix("Turn it off (restarts FPP)", disruptive=True)))
        else:
            out.append(_c("e131_out", "No leftover E1.31 broadcast", "ok", "Nothing extra on the network."))

    # 8. Output bus
    b = ctx.get("bus_stats") or {}
    tgt = b.get("target_fps") or getattr(s, "output_fps", 25)
    if b.get("ready") and (b.get("fps") or 0) >= 0.8 * tgt:
        out.append(_c("bus", "Lights output", "ok", f"{b.get('transport')} at {b.get('fps')} fps, {b.get('send_errors', 0)} send errors."))
    elif b:
        out.append(_c("bus", "Lights output", "fail",
                      f"Not sending ({b.get('last_error') or 'waiting for FPP'}).",
                      help="Usually fixed by the FPP checks above."))

    # 9. Audio
    a = ctx.get("audio_state") or {}
    cards = _usb_audio_cards()
    if a.get("ok"):
        out.append(_c("audio", "Sound output", "ok", f"Playing through {a.get('device') or 'the default device'}."))
    elif cards:
        idx, cid, desc = cards[0]
        out.append(_c("audio", "Sound output", "fail",
                      f"Found {desc}, but the app can't open it (often the Pi's HDMI port is the default and no monitor is attached).",
                      _fix(f"Use {desc.split(' at ')[0]} (restarts the app)", disruptive=True)))
    elif on_pi:
        out.append(_c("audio", "Sound output", "warn",
                      "No USB audio adapter found. (The Pi 5 has no headphone jack.)",
                      help="Plug in a USB audio adapter and reload this page."))
    else:
        out.append(_c("audio", "Sound output", "unknown", "Audio isn't running here."))

    # 10. Presence sensor
    sh = ctx.get("show_status") or {}
    if sh.get("gpio_pin") is None:
        out.append(_c("sensor", "Presence sensor", "warn", "No sensor pin set, so scares only fire by hand.",
                      help="Set the GPIO pin under Show → Presence sensor."))
    elif sh.get("gpio_ok") and sh.get("sensor_enabled") is False:
        out.append(_c("sensor", "Presence sensor", "warn",
                      f"GPIO {sh.get('gpio_pin')} is reading {'PRESENT' if sh.get('presence_active') else 'CLEAR'}, "
                      "but sensor triggers are switched off, so scares only fire by hand.",
                      help="Turn on \"Sensor fires scares\" under Show when you want it back."))
    elif sh.get("gpio_ok"):
        out.append(_c("sensor", "Presence sensor", "ok",
                      f"GPIO {sh.get('gpio_pin')}, reading {'PRESENT' if sh.get('presence_active') else 'CLEAR'}."))
    else:
        out.append(_c("sensor", "Presence sensor", "fail", f"GPIO {sh.get('gpio_pin')} couldn't be opened.",
                      help="Check the pin number and wiring; on a Pi 5, gpiozero needs the lgpio package."))

    # 11. Clock (dusk schedules depend on it)
    v = ctx.get("vitals") or {}
    if v.get("clock_synced") is False:
        out.append(_c("clock", "Clock is set", "warn", "The Pi's clock isn't synced, so dusk schedules may start at the wrong time."))
    elif v.get("clock_synced"):
        out.append(_c("clock", "Clock is set", "ok", "Synced over the network."))

    # 12. Power and heat
    th = v.get("throttled")
    if th is not None:
        now = [f for f in th["flags"] if f.endswith("now")]
        past = [f for f in th["flags"] if f.endswith("since boot")]
        if now:
            out.append(_c("power", "Power and temperature", "fail", "Right now: " + ", ".join(now) + ".",
                          help="Use the official Pi 5 27 W USB-C supply and give the Pi some airflow."))
        elif past:
            out.append(_c("power", "Power and temperature", "warn", "Since boot: " + ", ".join(past) + ".",
                          help="Usually a weak power supply or a long USB cable. The official 27 W supply fixes most of these."))
        else:
            out.append(_c("power", "Power and temperature", "ok",
                          f"No power or heat problems{', ' + str(v['temp_c']) + ' °C' if v.get('temp_c') is not None else ''}."))

    # 13. Disk
    if v.get("disk_free_gb") is not None and v["disk_free_gb"] < 1.0:
        out.append(_c("disk", "Free disk space", "warn", f"Only {v['disk_free_gb']} GB free. Audio uploads may fail."))
    return out


# ---------------------------------------------------------------- fixes + actions
def fix_boot_retry():
    php = php_fpm_unit()
    units = ["apache2.service"] + ([php] if php else [])
    for u in units:
        d = f"/etc/systemd/system/{u}.d"
        rc, _ = _sudo("mkdir", "-p", d)
        rc2, _ = _sudo("tee", f"{d}/{DROPIN_NAME}", input_text=DROPIN)
        if rc or rc2:
            return False, f"Couldn't write the override for {u} (does this user have sudo?)."
    _sudo("systemctl", "daemon-reload")
    return True, "Apache and PHP will now retry by themselves after a slow boot."


def restart_web():
    php = php_fpm_unit()
    units = ["apache2"] + ([php] if php else [])
    _sudo("systemctl", "reset-failed", *units, timeout=15)
    rc, out = _sudo("systemctl", "restart", *reversed(units), timeout=90)
    if rc != 0:
        return False, "Restart failed: " + (out.strip().splitlines() or ["no sudo?"])[-1]
    for _ in range(15):
        if _http("http://127.0.0.1/api/fppd/status")[0] == 200:
            return True, "FPP's web UI is back."
        time.sleep(1)
    return False, "Restarted, but FPP's web UI still isn't answering."


def restart_fppd():
    from .fpp.provision import _restart_fppd_guarded
    ok = _restart_fppd_guarded()
    return (True, "FPP restarted.") if ok else (False, "FPP didn't come back. Check FPP's own UI or reboot the Pi.")


def fix_ddp_input():
    path = f"{FPP_CFG}/ci-universes.json"
    cur = _read_root_json(path)
    data = cur if isinstance(cur, dict) else {}
    ins = data.setdefault("channelInputs", [])
    uni = next((i for i in ins if i.get("type") == "universes"), None)
    if uni is None:
        uni = {"type": "universes", "enabled": 1, "timeout": 1000, "universes": []}
        ins.append(uni)
    uni["enabled"] = 1
    uni.setdefault("timeout", 1000)         # blank the output 1 s after frames stop (dead-man switch)
    uni.setdefault("universes", [])
    if cur is not None:
        _write_root(path + ".haunt-backup", json.dumps(cur))
    if not _write_root(path, json.dumps(data)):
        return False, "Couldn't write FPP's input settings (does this user have sudo?)."
    for _ in range(10):                     # FPP picks the file up on its own
        st = _http(DIRECT_FPPD + "/fppd/status")[1] or {}
        if st.get("channelInputsEnabled"):
            return True, "FPP is listening for light data."
        time.sleep(1)
    return True, "Saved. If it still shows off in a minute, restart FPP."


def fix_e131_off():
    from .fpp.provision import _restart_fppd_guarded
    path = f"{FPP_CFG}/co-universes.json"
    cur = _read_root_json(path)
    if not isinstance(cur, dict):
        return False, "Couldn't read FPP's E1.31 settings."
    new = json.loads(json.dumps(cur))
    for o in new.get("channelOutputs") or []:
        if o.get("universes") and all(u.get("description") == OUR_E131_DESC for u in o["universes"]):
            o["enabled"] = 0
    _write_root(path + ".haunt-backup", json.dumps(cur))
    if not _write_root(path, json.dumps(new)):
        return False, "Couldn't write FPP's E1.31 settings."
    if _restart_fppd_guarded():
        return True, "Turned off; FPP restarted."
    _write_root(path, json.dumps(cur))      # put it back
    _restart_fppd_guarded()
    return False, "FPP didn't come back, so the change was reverted."


def fix_overlay(settings, block_end=None):
    from .fpp.provision import ensure_all
    end = int(block_end or settings.dmx_end_channel)
    r = ensure_all(settings.overlay_model_name, settings.dmx_start_channel,
                   end - settings.dmx_start_channel + 1, settings.e131_universe, with_output=False)
    if r.get("ok"):
        return True, "Created; FPP restarted."
    return False, "FPP didn't accept it" + (" and the change was reverted." if r.get("reverted") else ".")


def asoundrc_for(card_id):
    return ("# Haunt Director: send ALSA 'default' to the USB audio adapter (card id '%s').\n"
            "# Without this, default is the Pi's HDMI port, which fails with no monitor attached.\n"
            "pcm.!default {\n    type plug\n    slave.pcm \"hw:CARD=%s,DEV=0\"\n}\n"
            "ctl.!default {\n    type hw\n    card %s\n}\n") % (card_id, card_id, card_id)


def fix_audio(service_name):
    cards = _usb_audio_cards()
    if not cards:
        return False, "No USB audio adapter found."
    _, cid, desc = cards[0]
    rc_path = Path.home() / ".asoundrc"
    try:
        if rc_path.exists():
            rc_path.with_name(".asoundrc.bak-" + time.strftime("%Y%m%d-%H%M%S")).write_text(rc_path.read_text())
        rc_path.write_text(asoundrc_for(cid))
    except Exception as e:
        return False, f"Couldn't write {rc_path}: {e}"
    restart_app(service_name)
    return True, f"Sound now goes to {desc.split(' at ')[0]}. The app is restarting; this page reconnects by itself."


def restart_app(service_name):
    ok = _detached(f"sleep 1; sudo -n systemctl --no-block restart {service_name}")
    return ok, ("Restarting Haunt Director…" if ok else "Couldn't schedule the restart.")


def reboot():
    ok = _detached("sleep 2; sudo -n systemctl reboot")
    return ok, ("Rebooting the Pi. Give it a couple of minutes." if ok else "Couldn't schedule the reboot.")
