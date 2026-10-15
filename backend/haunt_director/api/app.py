"""FastAPI app — M1 vertical slice: health, manual control, STOP EVERYTHING."""
from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import subprocess
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..config import settings
from ..control import ControlService
from ..domain.scenes import Scene, SceneAudio, Event
from ..fpp import FppClient, FppError
from ..output import OutputBus, make_transport
from ..player import ShowPlayer, scene_audio_files, DUCK_DEFAULT_DB
from ..store import Store
from ..domain.resolve import capped_strobe_hz as _cap_hz


class SetFixture(BaseModel):
    rgb: tuple[int, int, int] | None = None
    brightness: int = Field(255, ge=0, le=255)
    strobe_hz: float | None = Field(None, ge=0, le=25)


class FogBurst(BaseModel):
    seconds: float = Field(3.0, gt=0, le=30)
    level: int = Field(255, ge=0, le=255)


# (The old 4 Hz fog watchdog is gone: a fog burst is now an expiring override
#  on the output bus, dropped on the first frame past its deadline.)


def _block_end(store) -> int:
    """Last DMX channel the output frame covers: HD_DMX_CHANNEL_COUNT (64 by
    default), grown to reach every device in the rig, at most 512."""
    from ..domain.rig import block_end
    return block_end(list(store.devices.values()), store.profiles, settings.dmx_end_channel)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    from ..domain.resolve import set_strobe_cap
    set_strobe_cap(settings.strobe_max_hz)          # optional global strobe ceiling
    # Provision FPP (Pi-local, best-effort). Skipped where FPP isn't installed, so a
    # dev laptop starts instantly instead of waiting on sudo and an absent fppd.
    from pathlib import Path as _Pfpp
    store = Store()
    app.state.store = store
    block = _block_end(store)        # the configured block, grown to reach every device (domain/rig.py)
    with contextlib.suppress(Exception):
        if not _Pfpp("/opt/fpp").exists():
            raise RuntimeError("no FPP here")
        from ..fpp.provision import ensure_all
        ensure_all(settings.overlay_model_name, settings.dmx_start_channel,
                   block - settings.dmx_start_channel + 1, settings.e131_universe,
                   with_output=(settings.output_kind == "e131"))
    fpp = FppClient(settings.fpp_base_url, settings.overlay_model_name, settings.fpp_timeout_s)
    # The ONE writer to FPP (output.py). Everything that affects lights or fog
    # -- scenes, Live cards, fog bursts, Stop -- is a layer on this bus.
    bus = OutputBus(store, make_transport(settings), fps=settings.output_fps,
                    nch=block,                             # frame covers DMX 1..end of the block
                    strobe_clear_frames=settings.strobe_clear_frames)
    app.state.bus = bus
    ctrl = ControlService(fpp, store, bus, settings.fog_max_seconds)
    app.state.ctrl = ctrl
    # audio engine (Pi-side). Fully optional: any failure -> silent no-op so the
    # light show never depends on it.
    # Audio init can probe ALSA slowly on a box with no sound card, so do it on a
    # background thread — the app must serve immediately regardless. The player
    # picks up the engine once it attaches; until then audio is simply silent.
    app.state.audio = None
    app.state.player = ShowPlayer(ctrl, bus, audio=None)
    app.state.player.voice_files = _voice_files     # which layers duck the music
    if settings.audio_enabled:
        import threading
        from pathlib import Path as _P

        def _init_audio():
            try:
                from ..audio import AudioEngine
                eng = AudioEngine(_P(__file__).resolve().parents[2] / settings.audio_dir,
                                  device=settings.audio_device)
                eng.prune_cache()                            # copies of clips that are gone
                _warm = []
                for _sc in store.scenes.values():
                    _warm += scene_audio_files(_sc)          # music, voice-over AND layer cues
                eng.warm_cache(list(dict.fromkeys(_warm)))
                app.state.audio = eng
                app.state.player.audio = eng
                eng.set_master(app.state.player.volume_gain)   # persisted app volume
                eng.set_duck_db(app.state.player.duck_db)      # persisted voice ducking
                # Scares must hit the instant someone walks up: keep their sounds
                # decoded in RAM (pinned) so a trigger never waits on the SD card.
                for _sc in store.scenes.values():
                    if getattr(_sc, "tag", "") == "scare" and getattr(_sc, "enabled", True):
                        eng.preload(scene_audio_files(_sc), pin=True)
            except Exception:
                pass

        threading.Thread(target=_init_audio, name="audio-init", daemon=True).start()
    # Show Director: unattended ambient + presence-triggered scares.
    from ..show import ShowDirector
    from ..store import DATA_DIR
    director = ShowDirector(app.state.player, store, ctrl, DATA_DIR / "show.json")
    director.attach_loop(asyncio.get_running_loop())
    # restore persisted global output guards (fog / audio) onto the player
    with contextlib.suppress(Exception):
        import json as _json
        _of = DATA_DIR / "output.json"
        if _of.exists():
            _od = _json.loads(_of.read_text())
            app.state.player.fog_enabled = bool(_od.get("fog_enabled", True))
            app.state.player.audio_muted = bool(_od.get("audio_muted", False))
            app.state.player.set_volume(_od.get("volume", 100))
            app.state.player.set_duck(_od.get("duck_db", DUCK_DEFAULT_DB))
            app.state.player.set_light_delay(_od.get("light_delay_ms", 0))
    bus.start()
    if director.gpio_pin is None and settings.presence_gpio_pin is not None:
        director.gpio_pin = settings.presence_gpio_pin
        director.gpio_active_high = settings.presence_gpio_active_high
    with contextlib.suppress(Exception):
        director.start_gpio()
    director.start_scheduler()
    app.state.show = director
    # House lights: lights only, their own schedule, underneath any scene (house.py)
    from ..house import HouseLights
    app.state.house = HouseLights(store, bus, app.state.player, director, DATA_DIR / "house.json")
    app.state.house.start()
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await app.state.house.stop()
        with contextlib.suppress(Exception):
            await app.state.show._stop_task()
        with contextlib.suppress(Exception):
            await app.state.player.stop()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(bus.stop)        # final frame: everything off
        with contextlib.suppress(Exception):
            await fpp.aclose()


app = FastAPI(title="Haunt Director", version="0.1.0", lifespan=lifespan)
# Same-origin by default: the UI is served from this app, so no browser needs
# cross-site access. (A wildcard here would let any web page a visitor opens on
# your Wi-Fi drive the rig.) Opt in with HD_CORS_ORIGINS for a separate frontend.
_cors = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if _cors:
    app.add_middleware(CORSMiddleware, allow_origins=_cors, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _same_origin_writes(request, call_next):
    """Refuse state-changing API calls that a browser made from ANOTHER site.
    Browsers attach an Origin header to cross-site POST/PUT/DELETE; same-site
    calls from this app carry our own origin, and scripts like curl send none."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin is not None:
            from urllib.parse import urlparse as _up
            from fastapi.responses import JSONResponse as _JR
            ok = (origin != "null" and _up(origin).netloc == request.headers.get("host", "")) or origin in _cors
            if not ok:
                return _JR({"detail": "cross-site request refused"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _revalidate_html(request, call_next):
    """Make browsers always revalidate the HTML shell so a normal refresh picks
    up new ?v= script versions (no hard-refresh needed). Static JS/CSS still cache."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _ctrl(app_) -> ControlService:
    return app_.state.ctrl


@app.get("/api/health")
async def health():
    ctrl = _ctrl(app)
    try:
        fpp = await ctrl.fpp.health()
    except FppError as e:
        fpp = {"ok": False, "error": str(e)}
    bus = getattr(app.state, "bus", None)
    return {"fpp": fpp, "devices": len(ctrl.store.devices),
            "output": bus.stats() if bus is not None else None}


@app.get("/api/devices")
async def devices():
    ctrl = _ctrl(app)
    return [d.model_dump() for d in ctrl.store.devices.values()]


@app.get("/api/profiles")
async def profiles():
    ctrl = _ctrl(app)
    return [p.model_dump() for p in ctrl.store.profiles.values()]


@app.post("/api/devices/{device_id}/set")
async def set_fixture(device_id: str, body: SetFixture):
    ctrl = _ctrl(app)
    try:
        values = await ctrl.set_fixture(device_id, rgb=body.rgb,
                                        brightness=body.brightness, strobe_hz=body.strobe_hz)
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    except (ValueError, FppError) as e:
        raise HTTPException(400, str(e))
    return {"device": device_id, "channels": values}


@app.post("/api/devices/{device_id}/off")
async def off_fixture(device_id: str):
    ctrl = _ctrl(app)
    try:
        await ctrl.blackout_fixture(device_id)
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    return {"device": device_id, "off": True}


@app.post("/api/devices/release")
async def release_all_fixtures():
    """Hand every manually-overridden fixture back to the scene."""
    await _ctrl(app).release_fixture(None)
    return {"released": "all"}


@app.post("/api/devices/{device_id}/release")
async def release_fixture(device_id: str):
    """Hand one fixture back to the scene (drop its Live-card override)."""
    try:
        await _ctrl(app).release_fixture(device_id)
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    return {"device": device_id, "released": True}


@app.post("/api/devices/{device_id}/fog")
async def fog_burst(device_id: str, body: FogBurst):
    if not getattr(app.state.player, "fog_enabled", True):
        raise HTTPException(409, "fog output is disabled")
    ctrl = _ctrl(app)
    try:
        secs = await ctrl.fog_burst(device_id, body.seconds, body.level)
    except KeyError:
        raise HTTPException(404, f"no device {device_id}")
    return {"device": device_id, "seconds": secs, "capped_at": ctrl.fog_max_seconds}


class OutputConfig(BaseModel):
    fog_enabled: bool | None = None
    audio_muted: bool | None = None
    volume: int | None = Field(None, ge=0, le=100)   # app master volume, %
    duck_db: int | None = Field(None, ge=0, le=18)   # voice ducking depth, dB (0 = off)
    light_delay_ms: int | None = Field(None, ge=-300, le=300)   # + = lights later than the sound


def _save_output_settings():
    with contextlib.suppress(Exception):
        import json as _json
        from ..store import DATA_DIR
        p = app.state.player
        (DATA_DIR / "output.json").write_text(_json.dumps(
            {"fog_enabled": p.fog_enabled, "audio_muted": p.audio_muted,
             "volume": p.volume, "duck_db": p.duck_db,
             "light_delay_ms": int(round(p.light_delay_s * 1000))}, indent=2))


def _output_state(p):
    from ..domain import resolve as _rs
    return {"fog_enabled": p.fog_enabled, "audio_muted": p.audio_muted, "volume": p.volume,
            "duck_db": p.duck_db, "light_delay_ms": int(round(p.light_delay_s * 1000)),
            "strobe_max_hz": _rs.STROBE_MAX_HZ or 0}       # the simulator caps its strobes the same way


@app.get("/api/output")
async def get_output():
    return _output_state(app.state.player)


@app.post("/api/output")
async def set_output(body: OutputConfig):
    p = app.state.player
    if body.fog_enabled is not None:
        p.fog_enabled = bool(body.fog_enabled)   # the bus zeroes fog on the next frame (<=40 ms)
    if body.audio_muted is not None:
        p.audio_muted = bool(body.audio_muted)
        if p.audio_muted and p.audio is not None:
            with contextlib.suppress(Exception):
                p.audio.stop()
    if body.volume is not None:
        p.set_volume(body.volume)                # live: everything already playing follows
    if body.duck_db is not None:
        p.set_duck(body.duck_db)                 # live: the envelope eases to the new depth
    if body.light_delay_ms is not None:
        p.set_light_delay(body.light_delay_ms)   # live: the next frame uses it
    _save_output_settings()
    return _output_state(p)


# Short clip for setting the outside speaker's level without starting a scene.
# HD_TEST_SOUND picks it; otherwise the bundled stinger, else any short "sfx-" clip.
def _test_sound_file(audio_dir):
    from pathlib import Path as _P
    d = _P(audio_dir)
    for name in (settings.test_sound, "sfx-stinger-cc0.mp3"):
        if name and (d / name).is_file():
            return name
    try:
        sfx = sorted(f.name for f in d.iterdir() if f.is_file() and f.name.lower().startswith("sfx")
                     and f.suffix.lower() in _AUD_EXT and f.stat().st_size < 4_000_000)
    except Exception:
        sfx = []
    return sfx[0] if sfx else None


@app.post("/api/output/test-sound")
async def output_test_sound():
    """Play a short clip through the Pi at the current master volume."""
    p = app.state.player
    eng = getattr(app.state, "audio", None)
    if eng is None or not eng.available:
        raise HTTPException(503, "audio not available")
    if p.audio_muted:
        raise HTTPException(409, "sound is off")
    f = _test_sound_file(eng.audio_dir)
    if f is None:
        raise HTTPException(404, "no test clip found")
    await asyncio.to_thread(eng.preload, [f])    # never decode on the event loop
    eng.play_cue(f, gain=1.0, loop=False, key="__voltest")
    return {"playing": f, "volume": p.volume}


SYNC_CLICK = ".sync-click.wav"      # hidden from the library (dot file)
SYNC_EVERY_S, SYNC_FIRST_S, SYNC_LEN_S = 0.5, 0.5, 12.5


def _sync_click_file(audio_dir):
    """A click every half second (made once with ffmpeg) for the sync test."""
    from pathlib import Path as _P
    f = _P(audio_dir) / SYNC_CLICK
    if f.exists() and f.stat().st_size > 1000:
        return f.name
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    expr = (f"0.9*sin(2*PI*1800*t)*exp(-90*mod(t-{SYNC_FIRST_S},{SYNC_EVERY_S}))*gte(t,{SYNC_FIRST_S})")
    try:
        subprocess.run([ff, "-v", "quiet", "-y", "-f", "lavfi", "-i",
                        f"aevalsrc='{expr}':s=44100:d={SYNC_LEN_S}", "-ac", "1", str(f)],
                       check=True, timeout=30)
    except Exception:
        return None
    return f.name if f.exists() else None


@app.post("/api/output/sync-test")
async def output_sync_test():
    """Light/sound sync check: a click every half second through the speaker
    while the first light flashes white on each click. Slide Light timing
    until they land together (it applies live)."""
    p = app.state.player
    d = _show(app)
    if d is not None and d.armed:
        raise HTTPException(409, "Disarm the show first.")
    if p.is_playing:
        raise HTTPException(409, "Stop the scene that's playing first.")
    eng = getattr(app.state, "audio", None)
    if eng is None or not eng.available:
        raise HTTPException(503, "audio not available")
    if p.audio_muted:
        raise HTTPException(409, "Sound is off. Turn it on to hear the clicks.")
    store = _ctrl(app).store
    lights = [x.id for x in store.devices.values()
              if x.kind.value == "dmx_fixture" and x.enabled and x.dmx_address is not None]
    if not lights:
        raise HTTPException(404, "no lights to flash")
    held = set()
    with contextlib.suppress(Exception):
        held = set(p.bus.manual_devices())          # a Live card would hide the flash
    lights = [x for x in lights if x not in held]
    if not lights:
        raise HTTPException(409, "Every light is set by hand on the Live tab. Release one first.")
    f = await asyncio.to_thread(_sync_click_file, eng.audio_dir)
    if not f:
        raise HTTPException(500, "couldn't make the click track (ffmpeg missing?)")
    evs, t = [], SYNC_FIRST_S
    i = 0
    while t < SYNC_LEN_S - 0.2:
        evs.append(Event(id=f"c{i}", t=round(t, 3), type="set_color",
                         params={"color": "#ffffff", "brightness": 1.0}))
        evs.append(Event(id=f"o{i}", t=round(t + 0.09, 3), type="set_brightness", params={"brightness": 0.0}))
        t += SYNC_EVERY_S; i += 1
    sc = Scene(id="__sync_test", name="Sync test", duration_s=SYNC_LEN_S,
               audio=SceneAudio(music=f, gain=1.0), tracks={lights[0]: evs})
    await p.play(sc)
    return {"playing": True, "light": lights[0], "seconds": SYNC_LEN_S,
            "light_delay_ms": int(round(p.light_delay_s * 1000))}


class Placement(BaseModel):
    sim_position: dict | None = None
    aim: dict | None = None
    spread: float | None = None


@app.put("/api/devices/{device_id}/placement")
async def set_placement(device_id: str, body: Placement):
    ctrl = _ctrl(app)
    dev = ctrl.store.device(device_id)
    if dev is None:
        raise HTTPException(404, f"no device {device_id}")
    if body.sim_position is not None:
        dev.sim_position = body.sim_position
    if body.aim is not None:
        dev.aim = body.aim
    if body.spread is not None:
        dev.spread = body.spread
    ctrl.store.save_devices()
    return {"saved": device_id}


class StaticConfig(BaseModel):
    static_color: str | None = None       # hex, one of the 8 presets
    static_mode: str | None = None        # "fixed" | "auto"
    static_brightness: float | None = None
    static_on: bool | None = None


@app.put("/api/devices/{device_id}/static")
async def set_static(device_id: str, body: StaticConfig):
    """Persist a standalone RGB spotlight's colour/mode (global, not per-scene).
    These lights aren't Pi-controllable; we only record what they're set to."""
    ctrl = _ctrl(app)
    dev = ctrl.store.device(device_id)
    if dev is None:
        raise HTTPException(404, f"no device {device_id}")
    if getattr(dev.kind, "value", dev.kind) != "static_rgb":
        raise HTTPException(400, "not a static RGB fixture")
    if body.static_color is not None:
        c = str(body.static_color).strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            raise HTTPException(400, "static_color must be a #rrggbb hex")
        dev.static_color = c.lower()
    if body.static_mode is not None:
        if body.static_mode not in ("fixed", "auto"):
            raise HTTPException(400, "static_mode must be 'fixed' or 'auto'")
        dev.static_mode = body.static_mode
    if body.static_brightness is not None:
        dev.static_brightness = max(0.0, min(1.0, float(body.static_brightness)))
    if body.static_on is not None:
        dev.static_on = bool(body.static_on)
    ctrl.store.save_devices()
    return {"saved": device_id, "static_color": dev.static_color, "static_mode": dev.static_mode,
            "static_brightness": dev.static_brightness, "static_on": dev.static_on}


# ---- the rig editor (System -> Your rig): add, change and remove devices ----
RIG_KINDS = ("dmx_fixture", "dmx_fog", "static_rgb")
IDENTIFY_S = 3.0


def _kind_of(d) -> str:
    return getattr(d.kind, "value", d.kind)


def _rig_busy() -> str | None:
    d = _show(app)
    if d is not None and d.armed:
        return "Disarm the show first, then change the rig."
    if app.state.player.is_playing:
        return "Stop the scene that's playing first, then change the rig."
    return None


def _rig_view() -> dict:
    from ..domain.rig import issues, needed_end, span, profile_kind
    store = _ctrl(app).store
    devs = list(store.devices.values())
    bus = getattr(app.state, "bus", None)
    out_devs = []
    for d in devs:
        sp = span(d, store.profiles)
        out_devs.append({**d.model_dump(), "span": list(sp) if sp else None})
    profs = []
    for p in store.profiles.values():
        profs.append({"id": p.id, "vendor": p.vendor, "model": p.model, "mode_name": p.mode_name,
                      "channel_count": p.channel_count, "kind": profile_kind(p), "tested": bool(p.tested),
                      "notes": p.notes, "source": store.profile_source.get(p.id, "library")})
    profs.sort(key=lambda x: (x["kind"] != "light", not x["tested"], x["vendor"] == "Generic", x["vendor"], x["model"]))
    busy = _rig_busy()
    return {"devices": out_devs, "profiles": profs,
            "block": {"start": settings.dmx_start_channel, "end": bus.nch if bus is not None else _block_end(store),
                      "configured_end": settings.dmx_end_channel, "needed_end": needed_end(devs, store.profiles)},
            "issues": issues(devs, store.profiles), "busy": busy is not None, "busy_reason": busy}


def _rig_commit(new_devices, touched=()) -> None:
    """Make a new device list live: save it, re-point the output bus (fog
    guard, strobe map, channel block), drop stale Live overrides, and let house
    lights pick up the change."""
    store = _ctrl(app).store
    store.replace_devices(new_devices)
    bus = getattr(app.state, "bus", None)
    if bus is not None:
        for dev_id in touched:
            bus.release_manual(dev_id)
        bus.refresh_devices()                        # fog guard covers any new fogger first...
        bus.ensure_channels(_block_end(store))       # ...then the frame grows to reach it
    h = getattr(app.state, "house", None)
    if h is not None:
        with contextlib.suppress(Exception):
            h.refresh_devices()


def _rig_check(new_devices, dev_id: str) -> None:
    """Refuse a change that leaves THIS device broken (other, older problems
    don't block it; they show up in the list and the System tab)."""
    from ..domain.rig import issues
    store = _ctrl(app).store
    errs = [i for i in issues(new_devices, store.profiles) if i["level"] == "error" and dev_id in i["devices"]]
    if errs:
        raise HTTPException(400, errs[0]["message"])


class RigDeviceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    kind: str = "dmx_fixture"
    profile_id: str | None = None
    dmx_address: int | None = Field(None, ge=1, le=512)
    follows: str | None = None
    enabled: bool = True


class RigDevicePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=40)
    profile_id: str | None = None
    dmx_address: int | None = Field(None, ge=1, le=512)
    follows: str | None = None                       # "" = follow nobody
    enabled: bool | None = None


@app.get("/api/rig")
async def rig_get():
    return _rig_view()


@app.get("/api/rig/suggest-address")
async def rig_suggest_address(profile_id: str, exclude: str | None = None):
    from ..domain.rig import suggest_address
    store = _ctrl(app).store
    p = store.profiles.get(profile_id)
    if p is None:
        raise HTTPException(404, f"no profile {profile_id}")
    return {"address": suggest_address(list(store.devices.values()), store.profiles, p.channel_count, exclude=exclude)}


@app.post("/api/rig/devices")
async def rig_add(body: RigDeviceIn):
    from ..domain.rig import new_id, suggest_address, default_placement
    from ..domain.devices import Device
    if (why := _rig_busy()):
        raise HTTPException(409, why)
    if body.kind not in RIG_KINDS:
        raise HTTPException(400, "A new device is a light, a fogger or a yard light.")
    store = _ctrl(app).store
    devs = list(store.devices.values())
    data = {"id": new_id(body.name, store.devices), "name": body.name.strip(), "kind": body.kind,
            "enabled": body.enabled, **default_placement(devs, body.kind)}
    if body.kind == "static_rgb":
        data["static_color"], data["static_brightness"] = "#fff4e0", 0.3
    else:
        prof = store.profiles.get(body.profile_id or "")
        if prof is None:
            raise HTTPException(400, "Pick a fixture profile for it.")
        addr = body.dmx_address or suggest_address(devs, store.profiles, prof.channel_count)
        if addr is None:
            raise HTTPException(400, "There's no room left in the DMX universe for it.")
        data.update(profile_id=prof.id, dmx_address=addr, follows=body.follows or None)
    dev = Device.model_validate(data)
    new = devs + [dev]
    _rig_check(new, dev.id)
    _rig_commit(new)
    return {"device": dev.model_dump(), "rig": _rig_view()}


@app.put("/api/rig/devices/{device_id}")
async def rig_update(device_id: str, body: RigDevicePatch):
    if (why := _rig_busy()):
        raise HTTPException(409, why)
    store = _ctrl(app).store
    cur = store.device(device_id)
    if cur is None:
        raise HTTPException(404, f"no device {device_id}")
    patch = body.model_dump(exclude_unset=True)
    if "name" in patch:
        patch["name"] = (patch["name"] or "").strip() or cur.name
    if "follows" in patch:
        patch["follows"] = patch["follows"] or None
    if _kind_of(cur) not in ("dmx_fixture", "dmx_fog"):
        patch = {k: v for k, v in patch.items() if k in ("name", "enabled")}
    elif "profile_id" in patch and patch["profile_id"] not in store.profiles:
        raise HTTPException(400, "That fixture profile isn't installed.")
    dev = cur.model_copy(update=patch)                # a new object: the output thread never sees a half-edit
    new = [dev if d.id == device_id else d for d in store.devices.values()]
    _rig_check(new, device_id)
    _rig_commit(new, touched=(device_id,))
    return {"device": dev.model_dump(), "rig": _rig_view()}


@app.delete("/api/rig/devices/{device_id}")
async def rig_delete(device_id: str):
    if (why := _rig_busy()):
        raise HTTPException(409, why)
    store = _ctrl(app).store
    if store.device(device_id) is None:
        raise HTTPException(404, f"no device {device_id}")
    new, unfollowed = [], []
    for d in store.devices.values():
        if d.id == device_id:
            continue
        if d.follows == device_id:                    # its followers now play only their own parts
            d = d.model_copy(update={"follows": None})
            unfollowed.append(d.name)
        new.append(d)
    _rig_commit(new, touched=(device_id,))
    return {"deleted": device_id, "unfollowed": unfollowed, "rig": _rig_view()}


@app.post("/api/rig/devices/{device_id}/identify")
async def rig_identify(device_id: str):
    """Flash one light bright white for a few seconds, so you can tell which
    one it is out in the yard."""
    if (why := _rig_busy()):
        raise HTTPException(409, why)
    from ..domain.resolve import resolve_fixture
    store = _ctrl(app).store
    dev = store.device(device_id)
    if dev is None:
        raise HTTPException(404, f"no device {device_id}")
    if _kind_of(dev) != "dmx_fixture":
        raise HTTPException(400, "Only lights can flash. Test a fogger with its Test button on Live.")
    prof = store.profile_for(dev)
    if prof is None or dev.dmx_address is None:
        raise HTTPException(400, f"{dev.name} needs a profile and an address first.")
    vals = resolve_fixture(prof, dev.dmx_address, rgb=(255, 255, 255), brightness=255)
    app.state.bus.set_manual(device_id, vals, expires_at=time.monotonic() + IDENTIFY_S)
    return {"device": device_id, "seconds": IDENTIFY_S}


@app.post("/api/stop")
async def stop_everything():
    """STOP EVERYTHING — disarm the show, cancel any scene, clear every override,
    all channels off. Always reachable. Disarming comes FIRST: otherwise an armed
    Show Director sees the player go idle and starts the next ambient scene."""
    h = getattr(app.state, "house", None)
    if h is not None:
        with contextlib.suppress(Exception):
            h.snooze()                  # first, so house lights can't fade in as the show stands down
    d = _show(app)
    if d is not None and d.armed:
        with contextlib.suppress(Exception):
            await d.arm(False)
    await app.state.player.stop()
    await _ctrl(app).stop_everything()
    return {"stopped": True, "disarmed": True}


# ---- scenes + live playback ----
@app.get("/api/scenes")
async def list_scenes():
    ctrl = _ctrl(app)
    scs = sorted(ctrl.store.scenes.values(), key=lambda s: (getattr(s, "order", 0), s.name))
    return [{"id": s.id, "name": s.name, "duration_s": s.duration_s,
             "blurb": s.blurb, "devices": list(s.tracks.keys()),
             "audio": s.audio.model_dump() if s.audio else None,
             "enabled": getattr(s, "enabled", True), "order": getattr(s, "order", 0),
             "tag": getattr(s, "tag", "")}
            for s in scs]


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "scene").lower()).strip("-")
    return s or "scene"


class NewScene(BaseModel):
    name: str | None = None
    duration_s: float | None = None


@app.post("/api/scenes")
async def create_scene(body: NewScene):
    store = _ctrl(app).store
    base = _slug(body.name or "new scene"); sid = base; n = 2
    while sid in store.scenes:
        sid = f"{base}-{n}"; n += 1
    order = 1 + max([getattr(s, "order", 0) for s in store.scenes.values()] or [0])
    sc = Scene(id=sid, name=(body.name or "New Scene"),
               duration_s=float(body.duration_s or 30.0), blurb="", tracks={}, order=order)
    store.save_scene(sc)
    return {"id": sid, "name": sc.name}


class GenReq(BaseModel):
    description: str
    duration_s: float = Field(30.0, ge=3, le=300)
    intensity_flow: str = "flat"     # flat | slow_build | build_strike | waves | cold_open
    name: str | None = None
    youtube_url: str | None = None   # optional: auto-clip a segment as the scene's audio
    beat_sync: bool = False          # lock the light events to the clip's detected beats
    song_show: bool = False          # drums/bass/guitar drive the lights, the lyrics pick the colours
    song: str | None = Field(None, max_length=160)   # "Artist - Title" when the video title is odd


async def _song_show(body, desc, key, model, provider, devices, store):
    """A song show: download the song, read the band and the lyrics, colour it
    with the AI, lay the lights on the beat. Saves and returns the scene."""
    from pathlib import Path as _P
    from .. import songshow as SS
    from ..generate import MAX_SCENE_S, _ask_json, sanitize_scene as _san, friendly_error
    yt = (body.youtube_url or "").strip()
    if not yt:
        raise HTTPException(400, "A song show needs a YouTube link to the song.")
    lights = [d for d, k in devices.items() if k == "dmx_fixture"]
    fogs = [d for d, k in devices.items() if k == "dmx_fog"]
    if not lights:
        raise HTTPException(400, "A song show needs at least one light.")
    ad = _P(__file__).resolve().parents[2] / settings.audio_dir
    with contextlib.suppress(Exception):
        ad.mkdir(parents=True, exist_ok=True)
    clip = await asyncio.to_thread(youtube_songclip, yt, min(float(body.duration_s), MAX_SCENE_S), ad)
    if not clip.get("ok"):
        raise HTTPException(502, "Couldn't fetch the song: " + clip.get("error", "download failed"))
    clipfile, cdur = clip["file"], float(clip["dur"])
    try:
        artist, track = SS.split_title(body.song or clip.get("title") or "")
        song_len = float(clip["full_dur"]) - float(clip["start"])
        ly = await asyncio.to_thread(SS.find_lyrics, artist, track, song_len)
        # The clip starts at the song's first note, like the album track the
        # timings were made against, so the times line up as they are.
        lines = list(ly["lines"]) if ly else []
        ask = lambda system, user: _ask_json(system, user, key, model, provider, max_tokens=4096)
        try:
            res = await asyncio.to_thread(SS.build, ad / clipfile, cdur, lines, track or clip.get("title"),
                                          artist, lights, fogs, ask, desc)
        except ValueError as e:
            raise HTTPException(502, "Couldn't find a steady beat in that song. Song shows work best on "
                                     "rock and pop with a clear drum beat.") from e

        def _safe():                               # thousands of events: off the event loop
            tr, _n, _b = _san({"tracks": res["tracks"]}, devices, cdur, "flat", settings.fog_max_seconds,
                              events_max=_beat_events_cap(cdur, len(lights)))
            return tr, {dev: [Event(**e) for e in evs] for dev, evs in tr.items()}
        tracks, ev_tracks = await asyncio.to_thread(_safe)
        if not tracks:
            raise HTTPException(502, "The song show came out empty. Try a different link.")
    except BaseException:
        with contextlib.suppress(Exception):
            (ad / clipfile).unlink()               # don't leave an unused clip behind
        raise
    _lib_upsert(clipfile, title=clip.get("title") or "YouTube clip", category="music", source="youtube", url=yt)
    _DUR_CACHE.pop(clipfile, None)
    eng = getattr(app.state, "audio", None)
    if eng is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(eng.warm_cache, [clipfile])
    name = (body.name or track or clip.get("title") or "Song Show")[:60]
    blurb = ("Lights follow the drums, bass and guitar; colours follow "
             + ("the lyrics" if res["lyrics_used"] else "the music")
             + (f" (~{round(res['tempo'])} BPM)." if res.get("tempo") else "."))
    base = _slug(name); sid = base; n = 2
    while sid in store.scenes:
        sid = f"{base}-{n}"; n += 1
    order = 1 + max([getattr(s, "order", 0) for s in store.scenes.values()] or [0])
    sc = Scene(id=sid, name=name, duration_s=round(cdur, 2), blurb=blurb, order=order,
               audio=SceneAudio(music=clipfile, gain=1.0), tracks=ev_tracks,
               gen={"mode": "song", "beat_sync": True, "description": desc, "youtube_url": yt,
                    "clip_file": clipfile, "clip_start": clip.get("start"), "tempo": res.get("tempo"),
                    "song": {"artist": artist, "track": track},
                    "lyrics": ({"source": "lrclib", "id": ly.get("id"), "lines": len(lines)} if ly else None),
                    "sections": res["sections"], "roles": res["roles"], "theme": res.get("theme"),
                    "downbeats": res.get("downbeats") or [],
                    "ending": _ending_tag(tracks, res.get("ending_from")),
                    "fogs": {f: sum(1 for e in tracks.get(f, []) if e["type"] == "fog") for f in fogs}})
    store.save_scene(sc)
    return {"id": sid, "name": sc.name, "blurb": blurb, "devices": list(tracks.keys()),
            "events": sum(len(v) for v in tracks.values()), "tempo": res.get("tempo"),
            "song": {"sections": len(res["sections"]), "lyrics": res["lyrics_used"],
                     "colors_from_ai": res["colors_from_ai"], "theme": res.get("theme"),
                     "whole_song": clip["full_dur"] - clip["start"] <= cdur + 1.0},
            "audio": {"file": clipfile, "title": clip.get("title"), "dur": cdur}}


BEAT_EVENTS_PER_LIGHT_S = 7                  # a busy drummer at ~150 BPM is about 5/s on one light
BEAT_EVENTS_MAX = 6000                       # a safety ceiling, well above any real song


def _beat_events_cap(dur, lights=3):
    """Beat sync and song shows lay a few events a second on each light (more
    on fast, busy songs); allow that with headroom. If it's ever exceeded, the
    sanitizer thins every fixture evenly across the scene, so the ending stays."""
    per_s = BEAT_EVENTS_PER_LIGHT_S * max(1, int(lights))
    return max(400, min(BEAT_EVENTS_MAX, int(float(dur) * per_s) + 200))


def _ending_tag(tracks, ending_from):
    """Which events are the song show's ending (the run-up strobe, the last
    hit, the fade to dark), so Extend can lift exactly those and nothing you
    added by hand. tracks: sanitized {device: [event dicts]}."""
    if not isinstance(ending_from, (int, float)):
        return None
    lo = round(float(ending_from), 2) - 0.011              # the sanitizer rounds times to 0.01 s
    ids = [e["id"] for evs in tracks.values() for e in evs if e["t"] >= lo]
    return {"from": round(max(0.0, lo), 3), "ids": ids} if ids else None


def _extend_events_cap(sc, span_s, lights, drop_ids, old):
    """Events the new part may add: the usual allowance for its length, but
    never taking the whole scene past BEAT_EVENTS_MAX."""
    kept = sum(1 for evs in sc.tracks.values() for e in evs if e.t < old and e.id not in drop_ids)
    return max(100, min(_beat_events_cap(span_s, lights), BEAT_EVENTS_MAX - kept))


def _merge_extension(data, add_tr, drop_ids, devices, old=None):
    """Fold Extend's new events into a scene dump (in place): lift the old
    events in drop_ids, and any past the old end (a scene shortened with the
    length slider keeps them, unplayable; they mustn't come back mid-song),
    keep clear air between the fog that's already there and the new part's,
    add the rest. Returns how many events were added."""
    from ..generate import FOG_GAP_S
    tracks = data["tracks"]
    for dev in list(tracks):
        tracks[dev] = [e for e in tracks[dev]
                       if e.get("id") not in drop_ids and (old is None or e["t"] < old)]
    added = 0
    for dev, evs in add_tr.items():
        if devices.get(dev) == "dmx_fog":
            olds = [(e["t"], e["t"] + e.get("duration_s", 0.0)) for e in tracks.get(dev, []) if e["type"] == "fog"]
            clear = lambda e: all(e["t"] >= b + FOG_GAP_S or e["t"] + e["duration_s"] + FOG_GAP_S <= a
                                  for a, b in olds)
            evs = [e for e in evs if e["type"] != "fog" or clear(e)]
        tracks.setdefault(dev, []).extend(evs)
        added += len(evs)
    for dev in tracks:
        tracks[dev].sort(key=lambda e: e["t"])
    return added


@app.post("/api/generate")
async def generate_scene(body: GenReq):
    """Author a new scene from a text description via the LLM, then SAVE it.

    The model proposes the choreography; generate.sanitize_scene GUARANTEES the
    result can never emit an unsafe strobe or an over-long/too-many fog burst,
    whatever the model returns. Runs off the event loop (the call takes seconds)."""
    from ..generate import generate as _gen, resolve_key
    store = _ctrl(app).store
    desc = (body.description or "").strip()
    if not desc and not body.song_show:
        raise HTTPException(400, "Describe the scene you want.")
    key, provider, model = resolve_key(settings)
    if not key:
        raise HTTPException(400, "No LLM API key configured (set HD_LLM_API_KEY in backend/.env).")
    devices = {d.id: d.kind.value for d in store.devices.values()
               if d.kind.value in ("dmx_fixture", "dmx_fog")}

    if body.song_show:
        return await _song_show(body, desc, key, model, provider, devices, store)

    # ---- beat-sync mode: lock the light show to the clip's detected beats ----
    if body.beat_sync:
        if not (body.youtube_url and body.youtube_url.strip()):
            raise HTTPException(400, "Beat sync needs a YouTube link to sync to.")
        from ..beats import detect_beats, compose_beatsync
        from ..generate import palette_from, sanitize_scene as _san
        from pathlib import Path as _P
        ad = _P(__file__).resolve().parents[2] / settings.audio_dir
        with contextlib.suppress(Exception):
            ad.mkdir(parents=True, exist_ok=True)
        from ..generate import MAX_SCENE_S
        bdur = min(float(body.duration_s), MAX_SCENE_S)
        clip = await asyncio.to_thread(youtube_autoclip, body.youtube_url.strip(), bdur, desc, ad)
        if not clip.get("ok"):
            raise HTTPException(502, "Couldn't fetch the audio to sync to: " + clip.get("error", "download failed"))
        clipfile = clip["file"]
        try:
            cdur = min((await asyncio.to_thread(_probe_dur, ad / clipfile)) or bdur, bdur)
            det = await asyncio.to_thread(detect_beats, str(ad / clipfile), cdur)
            if not det.get("beats"):
                raise HTTPException(502, "Couldn't find a clear beat in that clip. Try a different link, or use the non-synced generator.")
            pal, acc, mood = await asyncio.to_thread(palette_from, desc, key, model, provider)
            light_ids = [d for d, k in devices.items() if k == "dmx_fixture"]
            fog_ids = [d for d, k in devices.items() if k == "dmx_fog"]
            raw_tracks = compose_beatsync(det, pal, acc, light_ids, fog_ids, cdur, mood)
            bt, _bn, _bb = await asyncio.to_thread(_san, {"tracks": raw_tracks}, devices, cdur, "flat",
                                                   settings.fog_max_seconds,
                                                   events_max=_beat_events_cap(cdur, len(light_ids)))
            if not bt:
                raise HTTPException(502, "Beat sync produced no events. Try a different clip.")
        except BaseException:
            with contextlib.suppress(Exception):
                (ad / clipfile).unlink()           # don't leave an unused clip behind
            raise
        _lib_upsert(clipfile, title=clip.get("title") or "YouTube clip", category="music",
                    source="youtube", url=body.youtube_url.strip())
        _DUR_CACHE.pop(clipfile, None)
        eng = getattr(app.state, "audio", None)
        if eng is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(eng.warm_cache, [clipfile])
        bname = body.name or ("Beat Sync " + (clip.get("title") or "")[:40]).strip()
        bblurb = "Lights locked to the beat" + ((" (~%s BPM)" % det["tempo"]) if det.get("tempo") else "") + "."
        base = _slug(bname or "beat sync"); sid = base; n = 2
        while sid in store.scenes:
            sid = f"{base}-{n}"; n += 1
        order = 1 + max([getattr(s, "order", 0) for s in store.scenes.values()] or [0])
        sc = Scene(id=sid, name=(bname or "Beat Sync"), duration_s=round(cdur, 2), blurb=bblurb,
                   order=order, audio=SceneAudio(music=clipfile, gain=1.0),
                   tracks={dev: [Event(**e) for e in evs] for dev, evs in bt.items()},
                   gen={"description": desc, "beat_sync": True, "youtube_url": body.youtube_url.strip(),
                        "clip_file": clipfile, "clip_start": clip.get("start"),
                        "palette": pal, "accent": acc, "mood": mood})
        store.save_scene(sc)
        return {"id": sid, "name": sc.name, "blurb": bblurb, "devices": list(bt.keys()),
                "events": sum(len(v) for v in bt.values()), "tempo": det.get("tempo"),
                "audio": {"file": clipfile, "via": clip.get("via"), "title": clip.get("title")}}

    # The model writes the lights while the YouTube clip (if any) downloads.
    yt = (body.youtube_url or "").strip()
    ad = None
    if yt:
        from pathlib import Path as _P
        ad = _P(__file__).resolve().parents[2] / settings.audio_dir
        with contextlib.suppress(Exception):
            ad.mkdir(parents=True, exist_ok=True)

    async def _clip():
        if not yt:
            return None
        try:
            return await asyncio.to_thread(youtube_autoclip, yt, body.duration_s, desc, ad)
        except Exception as e:                       # audio is optional: report, don't fail
            return {"ok": False, "error": f"{type(e).__name__}"}

    gen_task = asyncio.to_thread(_gen, desc, body.duration_s, body.intensity_flow, devices,
                                 key, model, provider, settings.fog_max_seconds)
    gen_res, clip = await asyncio.gather(gen_task, _clip(), return_exceptions=True)
    if isinstance(clip, BaseException):
        clip = {"ok": False, "error": type(clip).__name__}
    def _drop_clip():
        if clip and clip.get("ok"):                  # don't leave an orphan clip behind
            with contextlib.suppress(Exception):
                (ad / clip["file"]).unlink()
    if isinstance(gen_res, BaseException):
        _drop_clip()
        from ..generate import friendly_error
        raise HTTPException(502, friendly_error(gen_res))
    tracks, gname, blurb = gen_res
    if not tracks:
        _drop_clip()
        raise HTTPException(502, "The model returned no usable events. Try again or reword the description.")
    # optional: the YouTube clip becomes the scene's music
    audio_obj = None
    audio_note = None
    if yt:
        if clip.get("ok"):
            audio_obj = SceneAudio(music=clip["file"], gain=1.0)
            audio_note = {"file": clip["file"], "via": clip.get("via"), "title": clip.get("title"),
                          "dur": clip.get("dur")}
            _lib_upsert(clip["file"], title=clip.get("title") or "YouTube clip",
                        category="music", source="youtube", url=yt)
            _DUR_CACHE.pop(clip["file"], None)
            eng = getattr(app.state, "audio", None)
            if eng is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(eng.warm_cache, [clip["file"]])
        else:
            audio_note = {"error": clip.get("error", "audio import failed")}
    base = _slug(body.name or gname or "generated scene"); sid = base; n = 2
    while sid in store.scenes:
        sid = f"{base}-{n}"; n += 1
    order = 1 + max([getattr(s, "order", 0) for s in store.scenes.values()] or [0])
    clip_ok = bool(yt and clip and clip.get("ok"))
    sc = Scene(id=sid, name=(body.name or gname or "Generated Scene"),
               duration_s=float(body.duration_s), blurb=blurb, order=order, audio=audio_obj,
               tracks={dev: [Event(**e) for e in evs] for dev, evs in tracks.items()},
               gen={"description": desc, "flow": body.intensity_flow, "beat_sync": False,
                    "youtube_url": yt or None, "clip_file": clip["file"] if clip_ok else None,
                    "clip_start": clip.get("start") if clip_ok else None})
    store.save_scene(sc)
    return {"id": sid, "name": sc.name, "blurb": blurb,
            "devices": list(tracks.keys()), "events": sum(len(v) for v in tracks.values()),
            "audio": audio_note}


# ---- Extend: make an existing scene longer --------------------------------
_CLIP_TAG = re.compile(r"^(?P<base>.+?)-t(?P<s>\d+)_(?P<e>\d+)(?:-\d+)?$")
_YT_ID = re.compile(r"^(?:yt|sfx-yt|vo-yt)-([A-Za-z0-9_-]{11})$")


def _pcm(path, start, length, sr=8000):
    """Mono 16-bit PCM of [start, start+length] of an audio file (b"" on failure)."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return b""
    try:
        r = subprocess.run([ff, "-v", "quiet", "-ss", f"{max(0.0, float(start)):.3f}",
                            "-t", f"{float(length):.3f}", "-i", str(path), "-ac", "1",
                            "-ar", str(sr), "-f", "s16le", "-"], capture_output=True, timeout=60)
        return r.stdout or b""
    except Exception:
        return b""


def _refine_clip_start(clip_path, src_path, approx, probe=2.0, search=1.0, sr=8000):
    """A clip's file name only records its start to the nearest second. Find the
    exact start by matching the clip's opening against the source around there,
    so the longer clip lines up with the lights already written (to the beat, for
    beat sync). Returns (start, matched)."""
    try:
        import audioop                        # stdlib through Python 3.12
    except ImportError:
        return float(approx), False
    ref = _pcm(clip_path, 0.0, probe, sr)
    lo = max(0.0, float(approx) - search)
    frag = _pcm(src_path, lo, probe + 2 * search, sr)
    if len(ref) < sr or len(frag) <= len(ref) or audioop.rms(ref, 2) < 30:
        return float(approx), False
    off, factor = audioop.findfit(frag, ref)
    seg = frag[off * 2: off * 2 + len(ref)]
    if len(seg) != len(ref):
        return float(approx), False
    resid = audioop.add(seg, audioop.mul(ref, 2, -factor), 2)
    if audioop.rms(resid, 2) > 0.35 * max(1, audioop.rms(seg, 2)):
        return float(approx), False          # not a confident match: keep the name's start
    return round(lo + off / sr, 3), True


def _music_origin(sc, d):
    """Where a scene's music came from, if a longer clip can be cut:
    {clip, local, url, start, exact, base}, or None (a whole song, or a file we
    can't trace back to its source)."""
    from pathlib import Path as _P
    mus = sc.audio.music if sc.audio else None
    if not mus or _P(mus).name != mus or mus.startswith("."):
        return None                           # only plain files in the audio folder
    clip = _P(d) / mus
    if not clip.exists():
        return None
    gen = sc.gen or {}
    m = _CLIP_TAG.match(_P(mus).stem)
    if gen.get("clip_file") == mus and gen.get("clip_start") is not None:
        start, exact = float(gen["clip_start"]), True
        base = m.group("base") if m else _P(mus).stem
    elif m:
        start, exact, base = float(m.group("s")), False, m.group("base")
    else:
        return None
    local = next((_P(d) / (base + ext) for ext in _AUD_EXT if (_P(d) / (base + ext)).exists()), None)
    lib = _load_audio_lib()
    url = ((gen.get("youtube_url") if gen.get("clip_file") == mus else None)
           or (lib.get(mus) or {}).get("url")
           or next(((lib.get(base + ext) or {}).get("url") for ext in _AUD_EXT
                    if (lib.get(base + ext) or {}).get("url")), None))
    if not url:
        # imports are named after the video id (yt-<id>, sfx-yt-<id>, vo-yt-<id>), so an
        # older clip whose library entry lost its link can still find its way back
        vid = _YT_ID.match(base)
        if vid:
            url = "https://www.youtube.com/watch?v=" + vid.group(1)
    if local is None and not url:
        return None
    return {"clip": clip, "local": local, "url": url, "start": start, "exact": exact, "base": base}


def extend_music(sc, new_dur, d):
    """A longer clip of the same song from the same start: the part the lights
    were written to plays exactly as before, and the song keeps going. Returns
    {ok, file, dur, start, url, title} | {ok: False, reason}. Stops at the end
    of the song if it's shorter than asked (dur says how long it really is)."""
    import shutil as _sh
    o = _music_origin(sc, d)
    if o is None:
        return {"ok": False, "reason": "not_extendable"}
    work = _work_dir(d)
    try:
        src, title = o["local"], None
        if src is None:
            dl = _yt_download(o["url"], "music", work)
            if not dl.get("ok"):
                return {"ok": False, "reason": "download", "error": dl.get("error")}
            src, title = work / dl["file"], dl.get("title")
        start, aligned = o["start"], True
        if not o["exact"]:
            start, aligned = _refine_clip_start(o["clip"], src, start)
        tmp = work / "ext.mp3"
        if not _ff_cut(src, start, new_dur, tmp):
            return {"ok": False, "reason": "trim"}
        dur = _probe_dur(tmp) or float(new_dur)
        name = _place(tmp, d, _clip_name(o["base"], start, start + dur))
        return {"ok": True, "file": name, "dur": dur, "start": start, "url": o["url"],
                "title": title, "aligned": aligned}
    finally:
        _sh.rmtree(work, ignore_errors=True)


def _beat_palette(sc, gen):
    """Palette for continuing a beat-sync scene: what it was made with, or (for
    older scenes) read back off its own events: the full-brightness downbeat
    flashes give the accent, the groove pops give the palette."""
    if gen.get("palette") or gen.get("accent"):
        return gen.get("palette"), gen.get("accent"), gen.get("mood") or "driving"
    from collections import Counter
    flashes, groove, gb = Counter(), [], []
    for evs in sc.tracks.values():
        for e in evs:
            c = (e.params or {}).get("color")
            if e.type != "set_color" or not c:
                continue
            b = float((e.params or {}).get("brightness", 1.0))
            if b >= 0.99:
                flashes[c.lower()] += 1
            else:
                gb.append(b)
                if c.lower() not in groove:
                    groove.append(c.lower())
    acc = flashes.most_common(1)[0][0] if flashes else None
    mood = "eerie" if gb and max(gb) <= 0.85 else "driving"
    return (groove[:5] or None), acc, mood


def _extend_context(sc, old, new_dur, note, devices, fog_left):
    """What the model needs to pick up where the scene leaves off."""
    from collections import Counter
    from ..domain.scenes import evaluate
    st = evaluate(sc, old)
    end_state = {}
    for dev, kind in devices.items():
        x = st.get(dev)
        if kind == "dmx_fog":
            end_state[dev] = "fog " + ("on" if x and x.fog else "off")
        elif x is None or x.brightness == 0:
            end_state[dev] = "dark"
        else:
            end_state[dev] = "#%02x%02x%02x at %d%%" % (*x.rgb, round(x.brightness / 2.55))
    colors, recent = Counter(), {}
    for dev, evs in sc.tracks.items():
        lines = []
        for e in sorted(evs, key=lambda x: x.t):
            c = (e.params or {}).get("color")
            if c:
                colors[c.lower()] += 1
            if e.t >= old - 20:
                b = (e.params or {}).get("brightness")
                lines.append(f"{e.t:.1f}s {e.type}" + (f" {c}" if c else "")
                             + (f" {int(round(float(b) * 100))}%" if b is not None else ""))
        if dev in devices:
            recent[dev] = lines[-8:]
    gen = sc.gen or {}
    return {"name": sc.name, "blurb": sc.blurb, "description": gen.get("description"),
            "note": note, "start": old, "end": new_dur, "end_state": end_state,
            "recent": recent, "palette": [c for c, _ in colors.most_common(6)], "fog_left": fog_left}


def _fmt_len(s):
    s = int(round(float(s)))
    return f"{s} s" if s < 60 else f"{s // 60}:{s % 60:02d}"


class ExtendReq(BaseModel):
    seconds: float = Field(60.0, ge=5, le=120)
    note: str | None = Field(None, max_length=300)
    from_s: float | None = None              # the length the editor saw; a mismatch means a stale copy


_EXT_LOCKS: dict = {}                        # scene id -> asyncio.Lock (one Extend at a time per scene)


@app.post("/api/scenes/{scene_id}/extend")
async def extend_scene(scene_id: str, body: ExtendReq):
    """Make a scene longer. New lights continue it (beat-locked for a beat-sync
    scene); YouTube music becomes a longer clip of the same song from the same
    start. The part of the scene that already exists is kept as it is, except
    a song show's ending (its run-up strobe, last hit and fade to dark): those
    exact events are lifted and the song carries on from where they began."""
    lock = _EXT_LOCKS.setdefault(scene_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "This scene is already being extended. Give it a moment.")
    async with lock:
        return await _extend(scene_id, body)


async def _extend(scene_id, body):
    from pathlib import Path as _P
    from ..generate import MAX_SCENE_S, extend_tracks, fog_bursts_max, resolve_key, friendly_error
    from ..generate import sanitize_scene as _san
    store = _ctrl(app).store
    sc = store.scenes.get(scene_id)
    if not sc:
        raise HTTPException(404, f"no scene {scene_id}")
    old = round(float(sc.duration_s), 2)
    if body.from_s is not None and abs(float(body.from_s) - old) > 0.01:
        raise HTTPException(409, "This scene changed somewhere else (another tab?). Reopen it and try again.")
    room = MAX_SCENE_S - old
    if room < 5:
        raise HTTPException(400, f"This scene is already {_fmt_len(old)} long, and scenes stop at 5 minutes.")
    want = round(old + min(float(body.seconds), room), 2)
    fingerprint = sc.model_dump_json()          # to spot an edit or delete while we work
    gen = dict(sc.gen or {})
    beat = bool(gen.get("beat_sync")) or (sc.blurb or "").startswith("Lights locked to the beat")
    devices = {d.id: d.kind.value for d in store.devices.values()
               if d.kind.value in ("dmx_fixture", "dmx_fog")}
    lights = [d for d, k in devices.items() if k == "dmx_fixture"]
    fogs = [d for d, k in devices.items() if k == "dmx_fog"]
    ad = _P(__file__).resolve().parents[2] / settings.audio_dir
    mus = sc.audio.music if sc.audio else None
    if mus and (_P(mus).name != mus or mus.startswith(".")):
        mus = None                              # only plain files in the audio folder
    cur_len = (await asyncio.to_thread(_probe_dur, ad / mus) or 0.0) if mus and (ad / mus).exists() else 0.0
    need_music = bool(mus) and cur_len < want - 0.5
    fog_used = max([sum(1 for e in sc.tracks.get(d, []) if e.type == "fog") for d in fogs] or [0])
    # Only the lights and fog this scene still uses: a lane you cleared stays clear.
    lights_in = [d for d in lights if sc.tracks.get(d)]
    made_fogs = gen.get("fogs") if isinstance(gen.get("fogs"), dict) else {}
    fogs_in = [d for d in fogs if any(e.type == "fog" for e in sc.tracks.get(d) or [])
               or made_fogs.get(d) == 0]           # it just never had a loud chorus to fog yet
    seam = old                                  # new events start here
    drop_ids: set = set()                       # old events to lift (a song show's ending)
    song = None                                 # a song show's new sections/roles/bar lines/ending
    m = None                                    # a NEW clip we made; dropped unless the scene is saved
    saved = False

    async def _music():
        if not need_music:
            return None
        try:
            return await asyncio.to_thread(extend_music, sc, want, ad)
        except Exception as e:
            return {"ok": False, "reason": type(e).__name__}

    try:
        if beat:
            m = await _music()
            if m and m.get("ok") and not m.get("aligned", True):
                raise HTTPException(409, "Couldn't line the longer clip up exactly with the old one, so the "
                                         "beat would drift. Nothing was changed.")
            if m and m.get("ok"):
                clip_file, clen = m["file"], (m.get("dur") or want)
            elif mus and cur_len > old + 3:
                clip_file, clen = mus, cur_len   # no more to fetch, but the song itself runs on
            elif not mus:
                raise HTTPException(400, "This beat-synced scene has no music to follow.")
            else:
                why = (m or {}).get("reason")
                raise HTTPException(400, {
                    "download": "Couldn't download the song again: " + str((m or {}).get("error") or "download failed"),
                    "trim": "Couldn't cut a longer clip of the song.",
                }.get(why, "This scene's song can't be made longer here (it's a whole track already, "
                           "or I can't tell where it came from), so there's no more beat to follow."))
            new_dur = round(min(want, clen), 2)
            if new_dur < old + 3:
                raise HTTPException(400, "The song ends here, so there's nothing left to sync to.")
            if gen.get("mode") == "song":
                # A song show keeps its sections, colours and light roles. The
                # band is read again on the longer clip, on the same bar lines,
                # and the rest of the song gets sections of its own (coloured
                # like the scene's own verse, chorus and so on).
                from .. import songshow as SS
                stored = [x for x in (gen.get("sections") if isinstance(gen.get("sections"), list) else [])
                          if isinstance(x, dict)]
                hint = gen.get("downbeats") if isinstance(gen.get("downbeats"), list) else []
                hint = hint or [x.get("start") for x in stored]
                ly = gen.get("lyrics") if isinstance(gen.get("lyrics"), dict) else {}
                lines = await asyncio.to_thread(SS.lyrics_by_id, ly.get("id")) if ly.get("id") is not None else []
                sm = await asyncio.to_thread(SS.analyze, str(ad / clip_file), new_dur, None, False, hint)
                if len(sm.get("beats") or []) < 8:
                    raise HTTPException(502, "No steady beat was found in the rest of the song.")
                stored_roles = gen.get("roles") if isinstance(gen.get("roles"), dict) else {}
                band = [d for d in stored_roles if d in lights_in] or lights_in
                roles = {d: stored_roles.get(d) for d in band}
                if not all(isinstance(r, str) and r in SS.ROLES for r in roles.values()):
                    roles = {}                          # garbled: hand the band out afresh (rig order)
                if not band:
                    raise HTTPException(400, "This song show has no lights left to play the band.")

                def _lay():
                    secs = SS.extend_sections(stored, sm, lines, old, new_dur)
                    raw, rl = SS.compose(sm, secs, [], band, fogs_in, new_dur, roles=roles or None)
                    return raw, {"sections": secs, "roles": rl, "downbeats": [round(x, 3) for x in sm["downbeats"]],
                                 "ending_from": SS.ending_from(sm, secs, new_dur)}
                raw, song = await asyncio.to_thread(_lay)
                # The old ending (a strobe run-up, the last hit, the fade to
                # dark) mustn't land mid-song: lift exactly those events, and
                # lay the song on from where they started. Anything you added
                # there by hand stays.
                tag = gen.get("ending") if isinstance(gen.get("ending"), dict) else {}
                f = tag.get("from")
                if isinstance(f, (int, float)) and not isinstance(f, bool) and 0 < f < old:
                    seam = round(float(f), 3)
                    ids = tag.get("ids") if isinstance(tag.get("ids"), list) else []
                    drop_ids = {i for i in ids if isinstance(i, str)}
                elif not tag:
                    # no record of the ending (an older song show): lift the
                    # generated events of the last three and a half bars (the
                    # run-up starts two bars before the last downbeat)
                    seam = max(0.0, round(old - 3.5 * 4 * (sm.get("period_s") or 0.5), 3))
                    drop_ids = {e.id for evs in sc.tracks.values() for e in evs
                                if e.t >= seam and str(e.id).startswith("g")}
            else:
                # plain beat sync has no ending to undo: the old part stays as it is
                from ..beats import detect_beats, compose_beatsync
                det = await asyncio.to_thread(detect_beats, str(ad / clip_file), new_dur)
                pal, acc, mood = _beat_palette(sc, gen)
                raw = await asyncio.to_thread(compose_beatsync, det, pal, acc, lights_in, fogs_in, new_dur, mood)
            fog_before = max([sum(1 for e in sc.tracks.get(d, []) if e.type == "fog" and e.id not in drop_ids
                                  and e.t < old) for d in fogs] or [0])
            add_tr, _n, _b = await asyncio.to_thread(
                _san, {"tracks": raw}, devices, new_dur, "flat", settings.fog_max_seconds,
                events_max=_extend_events_cap(sc, new_dur - seam, len(lights_in), drop_ids, old),
                fog_bursts=max(0, fog_bursts_max(new_dur) - fog_before), window=(seam, new_dur))
            if not any(add_tr.values()):
                raise HTTPException(502, "No beats were found in the rest of the song.")
        else:
            key, provider, model = resolve_key(settings)
            if not key:
                raise HTTPException(400, "No LLM API key configured (set HD_LLM_API_KEY in backend/.env).")
            new_dur = want
            ctx = _extend_context(sc, old, new_dur, body.note, devices,
                                  max(0, fog_bursts_max(new_dur) - fog_used))
            add_tr, m = await asyncio.gather(
                asyncio.to_thread(extend_tracks, ctx, devices, key, model, provider, settings.fog_max_seconds),
                _music(), return_exceptions=True)
            if isinstance(m, BaseException):
                m = {"ok": False, "reason": type(m).__name__}
            if isinstance(add_tr, BaseException):
                raise HTTPException(502, friendly_error(add_tr))
            if not any(add_tr.values()):
                raise HTTPException(502, "Nothing usable came back for the new part. Try again.")
        # Someone edited or deleted the scene while we worked? Then don't save
        # over their change; they can extend again from the new version.
        cur = store.scenes.get(scene_id)
        if cur is None:
            raise HTTPException(404, "The scene was deleted while it was being extended.")
        if cur.model_dump_json() != fingerprint:
            raise HTTPException(409, "The scene was edited while it was being extended, so nothing was "
                                     "saved. Try Extend again.")

        # Build the longer scene as a NEW object: if this scene is playing right
        # now, the running copy finishes untouched and the next play picks this up.
        data = cur.model_dump()
        added = _merge_extension(data, add_tr, drop_ids, devices, old)
        data["duration_s"] = new_dur
        if song:
            gen.update({"sections": song["sections"], "roles": song["roles"], "downbeats": song["downbeats"],
                        "ending": _ending_tag(add_tr, song["ending_from"]),
                        "fogs": {f: sum(1 for e in data["tracks"].get(f, []) if e["type"] == "fog")
                                 for f in fogs_in}})
        music_note = None
        if m and m.get("ok"):
            data["audio"]["music"] = m["file"]
            gen.update({"clip_file": m["file"], "clip_start": m["start"]})
            if m.get("url"):
                gen["youtube_url"] = m["url"]
            music_note = {"extended": True, "file": m["file"], "dur": m.get("dur"),
                          "loops": (m.get("dur") or 0) < new_dur - 0.5}
        elif mus:
            music_note = {"extended": False, "loops": cur_len < new_dur - 0.5}
        if gen:
            data["gen"] = gen
        new_sc = Scene.model_validate(data)
        store.save_scene(new_sc)
        saved = True
    finally:
        if not saved and m and m.get("ok"):
            with contextlib.suppress(Exception):
                (ad / m["file"]).unlink()      # don't leave an unused clip behind

    if m and m.get("ok"):
        entry = _load_audio_lib().get(mus) or {}
        _lib_upsert(m["file"], title=entry.get("title") or m.get("title") or _prettify_title(m["file"]),
                    category=entry.get("category") or "music",
                    source=entry.get("source") or ("youtube" if m.get("url") else None), url=m.get("url"))
        _DUR_CACHE.pop(m["file"], None)
        eng = getattr(app.state, "audio", None)
        if eng is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(eng.warm_cache, [m["file"]])
    return {"id": scene_id, "duration_s": new_dur, "added_s": round(new_dur - old, 2),
            "events": added, "beat": beat, "music": music_note}


@app.post("/api/scenes/{scene_id}/duplicate")
async def duplicate_scene(scene_id: str):
    store = _ctrl(app).store
    src = store.scenes.get(scene_id)
    if not src:
        raise HTTPException(404, f"no scene {scene_id}")
    base = _slug(src.name + " copy"); sid = base; n = 2
    while sid in store.scenes:
        sid = f"{base}-{n}"; n += 1
    data = src.model_dump(); data["id"] = sid; data["name"] = src.name + " (copy)"
    data["order"] = getattr(src, "order", 0) + 1
    sc = Scene.model_validate(data); store.save_scene(sc)
    return {"id": sid, "name": sc.name}


@app.delete("/api/scenes/{scene_id}")
async def remove_scene(scene_id: str):
    store = _ctrl(app).store
    if scene_id not in store.scenes:
        raise HTTPException(404, f"no scene {scene_id}")
    if app.state.player.status.get("scene") == scene_id:
        await app.state.player.stop()
    store.delete_scene(scene_id)
    return {"deleted": scene_id}


@app.get("/api/scenes/{scene_id}")
async def get_scene(scene_id: str):
    sc = _ctrl(app).store.scenes.get(scene_id)
    if not sc:
        raise HTTPException(404, f"no scene {scene_id}")
    return sc.model_dump()


class ScenePatch(BaseModel):
    name: str | None = None
    blurb: str | None = None
    duration_s: float | None = None
    enabled: bool | None = None
    order: int | None = None
    tag: str | None = None
    audio: SceneAudio | None = None
    audio_fade_out_s: float | None = None    # per-scene crossfade override (null = use global)
    tracks: dict[str, list[Event]] | None = None


@app.put("/api/scenes/{scene_id}")
async def save_scene(scene_id: str, patch: ScenePatch):
    store = _ctrl(app).store
    sc = store.scenes.get(scene_id)
    if not sc:
        raise HTTPException(404, f"no scene {scene_id}")
    data = patch.model_dump(exclude_unset=True)
    for f in ("name", "blurb", "duration_s", "enabled", "order", "tag", "audio_fade_out_s"):
        if f in data:
            setattr(sc, f, data[f])
    if "audio" in data:
        sc.audio = patch.audio       # SceneAudio instance or None (clear)
    if "tracks" in data and patch.tracks is not None:
        sc.tracks = patch.tracks     # full keyframe replace from the timeline editor
    store.save_scene(sc)
    return {"saved": scene_id}


@app.get("/api/scenes/{scene_id}/state")
async def scene_state(scene_id: str, t: float = 0.0):
    """Evaluate ANY scene at time t (for the editor's scrubbable preview)."""
    from ..domain.scenes import evaluate
    sc = _ctrl(app).store.scenes.get(scene_id)
    if not sc:
        raise HTTPException(404, f"no scene {scene_id}")
    out = {"t": t, "duration_s": sc.duration_s, "playing": False, "scene": scene_id, "devices": {}}
    for dev, st in evaluate(sc, max(0.0, min(float(t), sc.duration_s)), _ctrl(app).store.follows).items():
        out["devices"][dev] = {"hex": "#%02x%02x%02x" % st.rgb, "brightness": st.brightness,
                               "strobe": st.strobe_hz > 0, "strobe_hz": round(_cap_hz(st.strobe_hz), 2),
                               "fog": st.fog}
    return out


_DUR_CACHE: dict = {}      # filename -> (mtime, seconds)
_AUD_EXT = (".wav", ".mp3", ".ogg", ".m4a", ".flac")


# ---- audio library manifest: filename -> {title, category, source, url} ------
def _audio_lib_path():
    from pathlib import Path as _P
    return _P(__file__).resolve().parents[2] / "data" / "audio_library.json"


def _load_audio_lib():
    import json
    try:
        return json.loads(_audio_lib_path().read_text())
    except Exception:
        return {}


def _lib_upsert(file, title=None, category=None, source=None, url=None):
    import json, time as _t
    lib = _load_audio_lib()
    e = lib.get(file, {})
    if title:
        e["title"] = str(title)[:120]
    if category in ("music", "sfx", "voice"):
        e["category"] = category
    if source:
        e["source"] = source
    if url:
        e["url"] = str(url)[:400]
    e.setdefault("added_at", _t.time())
    lib[file] = e
    p = _audio_lib_path()
    with contextlib.suppress(Exception):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(lib, indent=2))
    return e


def _voice_files(files):
    """Of these audio files, the ones the library classes as spoken lines (the
    same rule the editor uses for its VOICE lanes). The player ducks under them."""
    lib = _load_audio_lib()
    return {f for f in files
            if f and ((lib.get(f) or {}).get("category") or _infer_category(f)) == "voice"}


def _infer_category(name):
    low = name.lower()
    if low.startswith("shadow") or "voice" in low or low.startswith("vo") or "-vo" in low:
        return "voice"
    if low.startswith("amb") or low.startswith("sfx"):
        return "sfx"
    return "music"


def _prettify_title(name):
    stem = name.rsplit(".", 1)[0]
    stem = re.sub(r"^(sfx-yt-|vo-yt-|yt-)", "", stem)      # import prefixes
    stem = re.sub(r"-t\d+_\d+(-\d+)?$", "", stem)          # trim tags like -t5_12
    stem = re.sub(r"[-_]+", " ", stem).strip()
    return (stem[:1].upper() + stem[1:]) if stem else name


def _probe_dur(path):
    ff = shutil.which("ffprobe")
    if not ff:
        return 0.0
    try:
        r = subprocess.run([ff, "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=10)
        return round(float((r.stdout or "0").strip() or 0), 2)
    except Exception:
        return 0.0


def _audio_durations(d):
    """Best-effort clip lengths via ffprobe, cached by mtime (0.0 if unknown)."""
    out = {}
    ff = shutil.which("ffprobe")
    if not d.is_dir():
        return out
    for f in d.iterdir():
        if not f.is_file() or f.suffix.lower() not in _AUD_EXT or f.name.startswith("."):
            continue
        try:
            mt = f.stat().st_mtime
        except Exception:
            continue
        c = _DUR_CACHE.get(f.name)
        if c and c[0] == mt:
            out[f.name] = c[1]; continue
        sec = 0.0
        if ff:
            try:
                r = subprocess.run([ff, "-v", "quiet", "-show_entries", "format=duration",
                                    "-of", "csv=p=0", str(f)],
                                   capture_output=True, text=True, timeout=10)
                sec = round(float((r.stdout or "0").strip() or 0), 2)
            except Exception:
                sec = 0.0
        _DUR_CACHE[f.name] = (mt, sec); out[f.name] = sec
    return out


@app.get("/api/audio")
async def list_audio():
    """Categorized audio library. Each entry carries a display title + source;
    files not in the manifest get an inferred category and a prettified title,
    so existing audio keeps working."""
    from pathlib import Path as _P
    d = _P(__file__).resolve().parents[2] / settings.audio_dir
    lib = _load_audio_lib()
    cats = {"music": [], "voice": [], "sfx": []}
    if d.is_dir():
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in _AUD_EXT:
                continue
            meta = lib.get(f.name, {})
            cat = meta.get("category") or _infer_category(f.name)
            if cat not in cats:
                cat = "music"
            cats[cat].append({"file": f.name,
                              "title": meta.get("title") or _prettify_title(f.name),
                              "source": meta.get("source", "file")})
    for c in cats:
        cats[c].sort(key=lambda x: x["title"].lower())
    return {"music": cats["music"], "voice": cats["voice"], "sfx": cats["sfx"],
            "dur": await asyncio.to_thread(_audio_durations, d)}


class YtImport(BaseModel):
    url: str
    kind: str = "music"          # music | sfx | voice


def _yt_download(url, kind, d):
    """Pull the audio track of a video URL to web/audio/ via yt-dlp (Pi has internet)."""
    import re, shutil, subprocess, sys as _sys
    from pathlib import Path as _P
    if not re.match(r"^https?://", url or ""):
        return {"ok": False, "error": "Enter a valid URL"}
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        cand = _P(_sys.executable).parent / "yt-dlp"
        ytdlp = str(cand) if cand.exists() else None
    if not ytdlp:
        return {"ok": False, "error": "yt-dlp is not installed on the Pi"}
    prefix = {"sfx": "sfx-yt-", "voice": "vo-yt-", "music": "yt-"}.get(kind, "yt-")
    tmpl = str(d / (prefix + "%(id)s.%(ext)s"))
    cmd = [ytdlp, "-x", "--audio-format", "mp3", "--audio-quality", "0",
           "--no-playlist", "--max-filesize", "60M", "--no-progress",
           "-o", tmpl, "--print", "after_move:filepath", "--print", "%(title)s", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=220)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Download timed out"}
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return {"ok": False, "error": (err[-1][:180] if err else "Download failed")}
    lines = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
    fp = next((l for l in lines if l.lower().endswith(".mp3")), (lines[-1] if lines else ""))
    title = next((l for l in lines if not l.lower().endswith(".mp3")), "")
    from pathlib import Path as _P2
    name = _P2(fp).name if fp else ""
    if not name:
        return {"ok": False, "error": "Could not locate the downloaded file"}
    return {"ok": True, "file": name, "title": (title or name), "kind": kind}


@app.post("/api/audio/youtube")
async def import_youtube(body: YtImport):
    from pathlib import Path as _P
    d = _P(__file__).resolve().parents[2] / settings.audio_dir
    with contextlib.suppress(Exception):
        d.mkdir(parents=True, exist_ok=True)
    res = await asyncio.to_thread(_yt_download, body.url, (body.kind or "music"), d)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "download failed"))
    _DUR_CACHE.pop(res["file"], None)      # so /api/audio reports the new clip's length
    _lib_upsert(res["file"], title=res.get("title"),
                category=(body.kind if body.kind in ("music", "sfx", "voice") else "music"),
                source="youtube", url=body.url)
    eng = getattr(app.state, "audio", None)
    if eng is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(eng.warm_cache, [res["file"]])
    return res


@app.post("/api/audio/upload")
async def upload_audio(request: Request, filename: str, title: str = "", category: str = "music"):
    """Save a user-uploaded audio file. Raw bytes in the body, metadata in the
    query string, so there is no python-multipart dependency."""
    from pathlib import Path as _P
    d = _P(__file__).resolve().parents[2] / settings.audio_dir
    with contextlib.suppress(Exception):
        d.mkdir(parents=True, exist_ok=True)
    name = _P(filename or "upload").name
    ext = _P(name).suffix.lower()
    if ext not in _AUD_EXT:
        raise HTTPException(400, "Unsupported audio type (use wav, mp3, ogg, m4a or flac).")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or ("upload" + ext)
    dest = d / safe; i = 2
    while dest.exists():
        dest = d / (_P(safe).stem + f"-{i}{ext}"); i += 1
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty upload.")
    if len(data) > 60 * 1024 * 1024:
        raise HTTPException(400, "File too large (60 MB max).")
    dest.write_bytes(data)
    cat = category if category in ("music", "sfx", "voice") else "music"
    disp = title.strip() or _prettify_title(dest.name)
    _lib_upsert(dest.name, title=disp, category=cat, source="upload")
    _DUR_CACHE.pop(dest.name, None)
    eng = getattr(app.state, "audio", None)
    if eng is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(eng.warm_cache, [dest.name])
    return {"ok": True, "file": dest.name, "title": disp, "category": cat}


class RegAudio(BaseModel):
    file: str
    title: str | None = None
    category: str = "music"
    source: str = "file"


@app.post("/api/audio/register")
async def register_audio(body: RegAudio):
    """Attach library metadata to a file already present in web/audio/ (used when
    a clip is copied straight into web/audio/ on the Pi)."""
    from pathlib import Path as _P
    d = _P(__file__).resolve().parents[2] / settings.audio_dir
    name = _P(body.file).name
    if not (d / name).exists():
        raise HTTPException(404, f"no audio file {name}")
    cat = body.category if body.category in ("music", "sfx", "voice") else "music"
    e = _lib_upsert(name, title=(body.title or _prettify_title(name)), category=cat, source=body.source)
    _DUR_CACHE.pop(name, None)
    eng = getattr(app.state, "audio", None)
    if eng is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(eng.warm_cache, [name])
    return {"ok": True, "file": name, **e}


def _scene_uses_audio(sc, name):
    """True if scene `sc` references audio file `name` as music, voiceover, or a layer."""
    a = getattr(sc, "audio", None)
    if not a:
        return False
    with contextlib.suppress(Exception):
        if getattr(a, "music", None) == name or getattr(a, "voiceover", None) == name:
            return True
        for cue in (getattr(a, "layers", None) or []):
            if getattr(cue, "file", None) == name:
                return True
    return False


class DelAudio(BaseModel):
    file: str
    force: bool = False          # delete even if scenes reference it


@app.post("/api/audio/delete")
async def delete_audio(body: DelAudio):
    """Remove a clip from the library: the source file, its normalized wav-cache
    copy, and its manifest entry. If any saved scenes reference the clip, refuse
    (need_force) unless force=True, and report the affected scenes either way."""
    import json
    from pathlib import Path as _P
    d = _P(__file__).resolve().parents[2] / settings.audio_dir
    if "/" in body.file or "\\" in body.file or ".." in body.file:
        raise HTTPException(400, "bad filename")
    name = _P(body.file).name
    if not name or _P(name).suffix.lower() not in _AUD_EXT:
        raise HTTPException(400, "bad filename")
    used = []
    with contextlib.suppress(Exception):
        for sc in _ctrl(app).store.scenes.values():
            if _scene_uses_audio(sc, name):
                used.append(sc.name)
    if used and not body.force:
        return {"ok": False, "need_force": True, "file": name, "used_by": used}
    with contextlib.suppress(Exception):
        src = d / name
        if src.exists():
            src.unlink()
    with contextlib.suppress(Exception):
        wc = d / "_wavcache" / (_P(name).stem + ".norm.wav")
        if wc.exists():
            wc.unlink()
    lib = _load_audio_lib()
    if name in lib:
        lib.pop(name, None)
        with contextlib.suppress(Exception):
            _audio_lib_path().write_text(json.dumps(lib, indent=2))
    _DUR_CACHE.pop(name, None)
    return {"ok": True, "file": name, "used_by": used}


class TrimReq(BaseModel):
    file: str
    start: float = 0.0
    end: float


def _audio_trim(file, start, end, d):
    """Cut [start,end] out of an audio file into a new clip (source untouched)."""
    import shutil, subprocess
    from pathlib import Path as _P
    name = _P(file).name
    if not name or "/" in file or ".." in file:
        return {"ok": False, "error": "bad filename"}
    src = d / name
    if not src.exists():
        return {"ok": False, "error": "file not found"}
    start = max(0.0, float(start)); end = float(end)
    if end - start < 0.2:
        return {"ok": False, "error": "Out must be at least 0.2s after In"}
    ff = shutil.which("ffmpeg")
    if not ff:
        return {"ok": False, "error": "ffmpeg missing"}
    tag = f"-t{int(round(start))}_{int(round(end))}"
    out = d / (src.stem + tag + ".mp3")
    i = 2
    while out.exists():
        out = d / (src.stem + tag + f"-{i}.mp3"); i += 1
    try:
        subprocess.run([ff, "-y", "-ss", f"{start}", "-i", str(src),
                        "-t", f"{end - start}", "-c:a", "libmp3lame", "-q:a", "2", str(out)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    except Exception:
        return {"ok": False, "error": "trim failed"}
    return {"ok": True, "file": out.name, "dur": round(end - start, 2)}


@app.post("/api/audio/trim")
async def trim_audio(body: TrimReq):
    from pathlib import Path as _P
    d = _P(__file__).resolve().parents[2] / settings.audio_dir
    res = await asyncio.to_thread(_audio_trim, body.file, body.start, body.end, d)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "trim failed"))
    _DUR_CACHE.pop(res["file"], None)
    eng = getattr(app.state, "audio", None)
    if eng is not None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(eng.warm_cache, [res["file"]])
    return res


# ---- automated YouTube clip selection for the scene generator ----------------
_GEN_STOP = set(("the a an and or of to in on at for with from into over under this that it "
                 "its is are was were be as by then when while very slow slowly build builds "
                 "hit end ending scene halloween that's you your they them their some more").split())


def _gen_keywords(description):
    ws = re.findall(r"[a-z]{4,}", (description or "").lower())
    seen, out = set(), []
    for w in ws:
        if w not in _GEN_STOP and w not in seen:
            seen.add(w); out.append(w)
    return out[:10]


def _vtt_seconds(s):
    parts = s.strip().replace(",", ".").split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        return None
    sec = 0.0
    for v in vals:
        sec = sec * 60 + v
    return sec


def _parse_vtt(path):
    """Return [(start_seconds, lowercased_text)] cue list from a .vtt file."""
    out = []
    try:
        lines = open(path, "r", encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        return out
    cur = None
    for ln in lines:
        if "-->" in ln:
            cur = _vtt_seconds(ln.split("-->")[0].strip().split(" ")[0])
        elif ln.strip() and cur is not None and "WEBVTT" not in ln and not ln.strip().isdigit():
            txt = re.sub(r"<[^>]+>", "", ln).strip().lower()
            if txt and txt not in ("[music]", "[applause]"):
                out.append((cur, txt))
    return out


def _yt_captions(url, d):
    import shutil, subprocess, glob
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return None
    tmpl = str(d / "cap-%(id)s")
    cmd = [ytdlp, "--skip-download", "--write-auto-subs", "--write-subs",
           "--sub-langs", "en.*", "--sub-format", "vtt", "--no-playlist", "-o", tmpl, url]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    hits = sorted(glob.glob(str(d / "cap-*.vtt")))
    return hits[0] if hits else None


def _energy_start(path, target_s):
    """Start-seconds of a high-energy window ~target_s long. Decodes to mono 8k
    s16le and scores per-0.5s RMS; picks randomly among the top few non-overlapping
    windows so re-generating varies the segment. Pure stdlib (audioop + ffmpeg)."""
    import shutil, subprocess, audioop, random
    ff = shutil.which("ffmpeg")
    if not ff:
        return 0.0
    try:
        r = subprocess.run([ff, "-v", "quiet", "-i", str(path), "-ac", "1",
                            "-ar", "8000", "-f", "s16le", "-"], capture_output=True, timeout=90)
        pcm = r.stdout
    except Exception:
        return 0.0
    frame = 8000  # 0.5s of mono int16 = 4000 samples * 2 bytes
    n = len(pcm) // frame
    if n < 2:
        return 0.0
    rms = [audioop.rms(pcm[i * frame:(i + 1) * frame], 2) for i in range(n)]
    win = max(1, int(round(target_s / 0.5)))
    if win >= n:
        return 0.0
    acc = sum(rms[:win]); windows = [(acc, 0)]
    for i in range(1, n - win + 1):
        acc += rms[i + win - 1] - rms[i - 1]
        windows.append((acc, i))
    windows.sort(reverse=True)
    top = windows[0][0] or 1
    chosen = []
    for score, idx in windows:
        if score < 0.80 * top:      # only genuinely high-energy segments qualify
            break                   # sorted desc, so nothing better remains
        if all(abs(idx - c) >= win for c in chosen):   # keep picks distinct
            chosen.append(idx)
        if len(chosen) >= 3:
            break
    return (random.choice(chosen) if chosen else 0) * 0.5


def _work_dir(d):
    """A private scratch folder inside the audio folder (same disk, so finished
    clips move into place instantly). Stale ones from a crash are swept."""
    import tempfile, shutil as _sh
    from pathlib import Path as _P
    with contextlib.suppress(Exception):
        for old in _P(d).glob(".clip-*"):
            if old.is_dir() and time.time() - old.stat().st_mtime > 3600:
                _sh.rmtree(old, ignore_errors=True)
    return _P(tempfile.mkdtemp(prefix=".clip-", dir=str(d)))


def _place(src, d, name):
    """Move a finished clip into the audio folder under `name`, never
    overwriting a file that's already there (adds -2, -3, ...)."""
    from pathlib import Path as _P
    stem, ext = _P(name).stem, (_P(name).suffix or ".mp3")
    out = _P(d) / (stem + ext); i = 2
    while out.exists():
        out = _P(d) / f"{stem}-{i}{ext}"; i += 1
    shutil.move(str(src), str(out))
    return out.name


def _ff_cut(src, start, length, out):
    """Encode [start, start+length] of src to an mp3 at out (stops at the end of src)."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    try:
        subprocess.run([ff, "-y", "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(src),
                        "-t", f"{float(length):.3f}", "-c:a", "libmp3lame", "-q:a", "2", str(out)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        return out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def _clip_name(base, start, end):
    return f"{base}-t{int(round(start))}_{int(round(end))}.mp3"


def youtube_songclip(url, target_s, d):
    """For a song show: the song from its first note, the whole song if it
    fits in target_s, else its first target_s seconds. Same private scratch
    folder as youtube_autoclip. Returns {ok, file, title, start, dur, full_dur}."""
    import shutil as _sh
    from pathlib import Path as _P
    from ..songshow import music_start
    work = _work_dir(d)
    try:
        dl = _yt_download(url, "music", work)
        if not dl.get("ok"):
            return dl
        src = work / dl["file"]
        full = _probe_dur(src)
        if not full:
            return {"ok": False, "error": "couldn't read the song"}
        start = music_start(src)
        length = max(4.0, min(float(target_s), full - start))
        tmp = work / "cut.mp3"
        if not _ff_cut(src, start, length, tmp):
            return {"ok": False, "error": "trim failed"}
        dur = _probe_dur(tmp) or round(length, 2)
        name = _place(tmp, d, _clip_name(_P(dl["file"]).stem, start, start + dur))
        return {"ok": True, "file": name, "title": dl.get("title", ""), "start": round(float(start), 3),
                "dur": dur, "full_dur": full}
    finally:
        _sh.rmtree(work, ignore_errors=True)


def youtube_autoclip(url, target_s, description, d, cap=None):
    """Download a YouTube clip and auto-pick a segment to fit the scene: use a
    caption line that matches the description if one exists, else the most intense
    passage. Trims to target_s (the whole song if it's shorter). Everything happens
    in a private scratch folder, so an existing full import of the same video
    (which other scenes may use) is never touched. Returns
    {ok, file, title, via, dur, start} | {ok: False, error}."""
    import shutil as _sh
    from pathlib import Path as _P
    from ..generate import MAX_SCENE_S
    cap = MAX_SCENE_S if cap is None else float(cap)
    work = _work_dir(d)
    try:
        dl = _yt_download(url, "music", work)
        if not dl.get("ok"):
            return dl
        src = work / dl["file"]
        target_s = max(4.0, min(float(target_s), cap))
        start, via = None, "energy"
        caps = _yt_captions(url, work)
        if caps:
            kws = _gen_keywords(description)
            for ts, txt in _parse_vtt(caps):
                if any(k in txt for k in kws):
                    start, via = max(0.0, ts - 1.0), "captions"; break
        if start is None:
            start = _energy_start(src, target_s)
        tmp = work / "cut.mp3"
        if not _ff_cut(src, start, target_s, tmp):
            return {"ok": False, "error": "trim failed"}
        dur = _probe_dur(tmp) or round(target_s, 2)
        name = _place(tmp, d, _clip_name(_P(dl["file"]).stem, start, start + dur))
        return {"ok": True, "file": name, "title": dl.get("title", ""), "via": via,
                "dur": dur, "start": round(float(start), 3)}
    finally:
        _sh.rmtree(work, ignore_errors=True)


@app.get("/api/audio/devices")
async def audio_devices():
    a = getattr(app.state, "audio", None)
    try:
        from ..audio import AudioEngine
        names = await asyncio.to_thread(AudioEngine.list_devices)   # ALSA probe off the loop
        return {"devices": names,
                "active": (a.device if a else None),
                "available": bool(a and a.available)}
    except Exception as e:
        return {"devices": [], "active": None, "available": False, "error": str(e)}


class PlayOpts(BaseModel):
    loop: bool = False
    mute: list[str] = []          # lane keys to silence: device ids, 'music', 'voiceover', cue ids


def _refuse_while_armed():
    """While the show is armed the yard belongs to it: playing, seeking or
    muting a scene from the editor would cut into the show. (The app plays it
    in the simulator instead.)"""
    d = _show(app)
    if d is not None and d.armed:
        raise HTTPException(409, "The show is armed, so Play runs in the simulator only. "
                                 "Disarm the show to play on the yard.")


@app.post("/api/scenes/{scene_id}/play")
async def play_scene(scene_id: str, opts: PlayOpts | None = None):
    ctrl = _ctrl(app)
    sc = ctrl.store.scenes.get(scene_id)
    if not sc:
        raise HTTPException(404, f"no scene {scene_id}")
    _refuse_while_armed()
    await app.state.player.play(sc, loop=(opts.loop if opts else False), mute=(opts.mute if opts else []))
    return {"playing": scene_id}


class PlayMute(BaseModel):
    mute: list[str] = []


@app.post("/api/play/mute")
async def play_mute(body: PlayMute):
    """Silence/restore lanes on the currently-playing scene, live."""
    _refuse_while_armed()
    app.state.player.set_mute(body.mute)
    return {"ok": True, "mute": body.mute}


class PlaySeek(BaseModel):
    t: float = 0.0


@app.post("/api/play/seek")
async def play_seek(body: PlaySeek):
    """Jump the currently-playing scene to time t."""
    _refuse_while_armed()
    app.state.player.seek(body.t)
    return {"ok": True, "t": body.t}


@app.get("/api/play/audiostate")
async def play_audiostate():
    a = getattr(app.state, "audio", None)
    return a.debug_state() if a else {"ok": False, "audio": None}


@app.get("/api/play/status")
async def play_status():
    return app.state.player.status


@app.get("/api/play/state")
async def play_state():
    """Live per-device colour for the mobile preview (trimmed simulator)."""
    from ..domain.scenes import evaluate
    p = app.state.player
    st = p.status
    out = {"t": st["t"], "playing": st["playing"], "scene": st["scene"], "devices": {}}
    sc = _ctrl(app).store.scenes.get(st["scene"]) if st["scene"] else None
    if sc:
        for dev, s in evaluate(sc, p._t, _ctrl(app).store.follows).items():
            hexc = "#%02x%02x%02x" % s.rgb
            out["devices"][dev] = {"hex": hexc, "brightness": s.brightness,
                                   "strobe": s.strobe_hz > 0, "strobe_hz": round(_cap_hz(s.strobe_hz), 2),
                                   "fog": s.fog}
    h = getattr(app.state, "house", None)
    if h is not None and h.active and not st["playing"]:
        lvl = h._level(time.monotonic())
        out["house"] = True
        out["devices"] = {k: {"hex": "#%02x%02x%02x" % rgb, "brightness": int(bri * lvl + 0.5),
                              "strobe": False, "strobe_hz": 0, "fog": 0}
                          for k, (rgb, bri) in h.colors_now().items()}
    bus = getattr(app.state, "bus", None)
    out["overrides"] = bus.manual_devices() if bus is not None else []
    # live presence sensor state for the top signal strip (polled ~3x/sec)
    d = _show(app)
    if d is not None:
        pa = d.presence_active
        out["presence"] = {"active": pa, "connected": pa is not None, "armed": bool(d.armed),
                           "enabled": bool(d.sensor_enabled)}
    return out


# ---- house lights (house.py) ----
class HouseCfg(BaseModel):
    mode: str | None = None
    brightness: float | None = Field(None, ge=0, le=1)
    hold_min: float | None = Field(None, ge=1, le=60)
    lights: dict | None = None
    windows: list | None = None


class HouseMode(BaseModel):
    mode: str


def _house():
    h = getattr(app.state, "house", None)
    if h is None:
        raise HTTPException(503, "House lights aren't running yet")
    return h


@app.get("/api/house")
async def house_get():
    return _house().status()


@app.put("/api/house")
async def house_put(body: HouseCfg):
    if body.mode is not None and body.mode not in ("off", "schedule", "on"):
        raise HTTPException(400, "mode is off, schedule or on")
    return _house().set_config(body.model_dump(exclude_none=True))


@app.post("/api/house/mode")
async def house_mode(body: HouseMode):
    if body.mode not in ("off", "schedule", "on"):
        raise HTTPException(400, "mode is off, schedule or on")
    return _house().set_mode(body.mode)


@app.post("/api/play/stop")
async def play_stop():
    await app.state.player.stop()
    return {"stopped": True}


class ShowArm(BaseModel):
    armed: bool


class ShowConfig(BaseModel):
    cooldown_s: float | None = None
    crossfade_s: float | None = None
    gpio_pin: int | None = None
    gpio_active_high: bool | None = None
    clear_gpio: bool = False
    ambient_enabled: bool | None = None       # False = sensor-only show (dark between scares)
    sensor_enabled: bool | None = None        # False = the sensor never fires a scare
    presence_hold_s: float | None = Field(None, ge=0, le=5)


def _show(app_):
    return getattr(app_.state, "show", None)


@app.get("/api/show")
async def show_status():
    d = _show(app)
    return d.status() if d else {"armed": False, "state": "unavailable"}


@app.post("/api/show/arm")
async def show_arm(body: ShowArm):
    d = _show(app)
    if d is None:
        raise HTTPException(503, "show director unavailable")
    await d.arm(body.armed)
    return d.status()


@app.post("/api/show/config")
async def show_config(body: ShowConfig):
    d = _show(app)
    if d is None:
        raise HTTPException(503, "show director unavailable")
    pin = None if body.clear_gpio else (body.gpio_pin if body.gpio_pin is not None else ...)
    d.set_config(cooldown_s=body.cooldown_s, gpio_pin=pin, gpio_active_high=body.gpio_active_high,
                 crossfade_s=body.crossfade_s, ambient_enabled=body.ambient_enabled,
                 sensor_enabled=body.sensor_enabled, presence_hold_s=body.presence_hold_s)
    return d.status()


class ShowSchedule(BaseModel):
    enabled: bool | None = None
    lat: float | None = None
    lng: float | None = None
    windows: list | None = None


@app.post("/api/show/schedule")
async def show_schedule(body: ShowSchedule):
    d = _show(app)
    if d is None:
        raise HTTPException(503, "show director unavailable")
    d.set_schedule(enabled=body.enabled, lat=body.lat, lng=body.lng, windows=body.windows)
    return d.status()


@app.post("/api/show/trigger")
async def show_trigger():
    d = _show(app)
    if d is None:
        raise HTTPException(503, "show director unavailable")
    d.trigger()
    return {"triggered": True, "state": d.state}


# --- static frontend (mounted last so /api/* wins) ---
# ---- FPP Health: live status, setup checker with one-click fixes, maintenance ----
from .. import system as _sys  # noqa: E402
from typing import Literal  # noqa: E402

_SYS_LOCK = asyncio.Lock()          # one fix / restart at a time


def _audio_state():
    a = getattr(app.state, "audio", None)
    try:
        return a.debug_state() if a is not None else {"ok": False}
    except Exception:
        return {"ok": False}


def _show_status():
    try:
        return app.state.show.status()
    except Exception:
        return {}


def _show_busy() -> bool:
    sh = _show_status()
    return bool(sh.get("armed")) or bool(app.state.player.is_playing)


@app.get("/api/system/health")
async def system_health():
    fpp_info, vit, svc = await asyncio.gather(
        asyncio.to_thread(_sys.fpp_status, settings.fpp_base_url),
        asyncio.to_thread(_sys.vitals),
        asyncio.to_thread(_sys.services))
    a, sh = _audio_state(), _show_status()
    bus = getattr(app.state, "bus", None)
    return {"fpp": fpp_info, "vitals": vit, "services": svc,
            "output": bus.stats() if bus is not None else None,
            "audio": {k: a.get(k) for k in ("ok", "device", "master")},
            "sensor": {"pin": sh.get("gpio_pin"), "ok": sh.get("gpio_ok"),
                       "present": sh.get("presence_active")},
            "show": {"armed": sh.get("armed"), "state": sh.get("state"),
                     "playing": app.state.player.is_playing},
            "busy": _SYS_LOCK.locked()}


@app.get("/api/system/checks")
async def system_checks():
    fpp_info, vit = await asyncio.gather(
        asyncio.to_thread(_sys.fpp_status, settings.fpp_base_url),
        asyncio.to_thread(_sys.vitals))
    bus = getattr(app.state, "bus", None)
    from ..domain.rig import issues as _rig_issues, needed_end as _needed_end
    store = _ctrl(app).store
    devs = list(store.devices.values())
    ctx = {"settings": settings, "fpp": fpp_info, "vitals": vit,
           "bus_stats": bus.stats() if bus is not None else {},
           "audio_state": _audio_state(), "show_status": _show_status(),
           "rig": {"block_end": bus.nch if bus is not None else _block_end(store),
                   "needed_end": _needed_end(devs, store.profiles),
                   "issues": _rig_issues(devs, store.profiles),
                   "lights": sum(1 for d in devs if getattr(d.kind, "value", d.kind) == "dmx_fixture"),
                   "foggers": sum(1 for d in devs if getattr(d.kind, "value", d.kind) == "dmx_fog")}}
    checks = await asyncio.to_thread(_sys.run_checks, ctx)
    counts = {k: sum(1 for c in checks if c["status"] == k) for k in ("ok", "warn", "fail", "unknown")}
    return {"checks": checks, "counts": counts}


# check id -> (disruptive?, fix). Disruptive fixes restart FPP or this app, so
# they wait until the show is disarmed and no scene is playing.
_FIXES = {
    "fppd":       (True,  lambda: _sys.restart_fppd()),
    "fpp_web":    (False, lambda: _sys.restart_web()),
    "boot_retry": (False, lambda: _sys.fix_boot_retry()),
    "overlay":    (True,  lambda: _sys.fix_overlay(settings, _block_end(_ctrl(app).store))),
    "ddp_in":     (False, lambda: _sys.fix_ddp_input()),
    "e131_out":   (True,  lambda: _sys.fix_e131_off()),
    "audio":      (True,  lambda: _sys.fix_audio(settings.service_name)),
}


class SysConfirm(BaseModel):
    # A JSON body (not an empty POST) means a browser must preflight any
    # cross-site attempt, which same-origin CORS then refuses.
    confirm: bool = False


@app.post("/api/system/checks/{check_id}/fix")
async def system_fix(check_id: str, body: SysConfirm):
    if check_id not in _FIXES:
        raise HTTPException(404, f"no fix for {check_id}")
    if not body.confirm:
        raise HTTPException(400, "confirm required")
    disruptive, fn = _FIXES[check_id]
    if disruptive and _show_busy():
        raise HTTPException(409, "Disarm the show and stop the scene first: this fix restarts FPP or the app.")
    if _SYS_LOCK.locked():
        raise HTTPException(409, "Another fix or restart is still running.")
    async with _SYS_LOCK:
        ok, detail = await asyncio.to_thread(fn)
    return {"ok": ok, "detail": detail}


class SysAction(BaseModel):
    action: Literal["restart_web", "restart_fppd", "restart_app", "reboot"]
    confirm: bool = False


@app.post("/api/system/action")
async def system_action(body: SysAction):
    """Maintenance buttons. Allowed mid-show (that's often exactly when you need
    them); the UI asks twice, and the lights fail safe (dark, fog off) if FPP
    or the app goes down."""
    if not body.confirm:
        raise HTTPException(400, "confirm required")
    if _SYS_LOCK.locked():
        raise HTTPException(409, "Another fix or restart is still running.")
    async with _SYS_LOCK:
        if body.action == "restart_web":
            ok, detail = await asyncio.to_thread(_sys.restart_web)
        elif body.action == "restart_fppd":
            ok, detail = await asyncio.to_thread(_sys.restart_fppd)
        elif body.action == "restart_app":
            ok, detail = _sys.restart_app(settings.service_name)
        else:
            ok, detail = _sys.reboot()
    return {"ok": ok, "detail": detail, "action": body.action}


from pathlib import Path  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_WEB = Path(__file__).resolve().parents[2] / "web"
if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
