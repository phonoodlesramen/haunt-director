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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    from ..domain.resolve import set_strobe_cap
    set_strobe_cap(settings.strobe_max_hz)          # optional global strobe ceiling
    # Provision FPP (Pi-local, best-effort). Skipped where FPP isn't installed, so a
    # dev laptop starts instantly instead of waiting on sudo and an absent fppd.
    from pathlib import Path as _Pfpp
    with contextlib.suppress(Exception):
        if not _Pfpp("/opt/fpp").exists():
            raise RuntimeError("no FPP here")
        from ..fpp.provision import ensure_all
        ensure_all(settings.overlay_model_name, settings.dmx_start_channel,
                   settings.dmx_channel_count, settings.e131_universe,
                   with_output=(settings.output_kind == "e131"))
    fpp = FppClient(settings.fpp_base_url, settings.overlay_model_name, settings.fpp_timeout_s)
    store = Store()
    # The ONE writer to FPP (output.py). Everything that affects lights or fog
    # -- scenes, Live cards, fog bursts, Stop -- is a layer on this bus.
    bus = OutputBus(store, make_transport(settings), fps=settings.output_fps,
                    nch=settings.dmx_end_channel,          # frame covers DMX 1..end of the block
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
    bus.start()
    if director.gpio_pin is None and settings.presence_gpio_pin is not None:
        director.gpio_pin = settings.presence_gpio_pin
        director.gpio_active_high = settings.presence_gpio_active_high
    with contextlib.suppress(Exception):
        director.start_gpio()
    director.start_scheduler()
    app.state.show = director
    try:
        yield
    finally:
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


def _save_output_settings():
    with contextlib.suppress(Exception):
        import json as _json
        from ..store import DATA_DIR
        p = app.state.player
        (DATA_DIR / "output.json").write_text(_json.dumps(
            {"fog_enabled": p.fog_enabled, "audio_muted": p.audio_muted,
             "volume": p.volume, "duck_db": p.duck_db}, indent=2))


@app.get("/api/output")
async def get_output():
    p = app.state.player
    return {"fog_enabled": p.fog_enabled, "audio_muted": p.audio_muted, "volume": p.volume,
            "duck_db": p.duck_db}


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
    _save_output_settings()
    return {"fog_enabled": p.fog_enabled, "audio_muted": p.audio_muted, "volume": p.volume,
            "duck_db": p.duck_db}


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


@app.post("/api/stop")
async def stop_everything():
    """STOP EVERYTHING — disarm the show, cancel any scene, clear every override,
    all channels off. Always reachable. Disarming comes FIRST: otherwise an armed
    Show Director sees the player go idle and starts the next ambient scene."""
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


@app.post("/api/generate")
async def generate_scene(body: GenReq):
    """Author a new scene from a text description via the LLM, then SAVE it.

    The model proposes the choreography; generate.sanitize_scene GUARANTEES the
    result can never emit an unsafe strobe or an over-long/too-many fog burst,
    whatever the model returns. Runs off the event loop (the call takes seconds)."""
    from ..generate import generate as _gen, resolve_key
    store = _ctrl(app).store
    desc = (body.description or "").strip()
    if not desc:
        raise HTTPException(400, "Describe the scene you want.")
    key, provider, model = resolve_key(settings)
    if not key:
        raise HTTPException(400, "No LLM API key configured (set HD_LLM_API_KEY in backend/.env).")
    devices = {d.id: d.kind.value for d in store.devices.values()
               if d.kind.value in ("dmx_fixture", "dmx_fog")}

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
        bdur = min(float(body.duration_s), 60.0)
        clip = await asyncio.to_thread(youtube_autoclip, body.youtube_url.strip(), bdur, desc, ad, 60.0)
        if not clip.get("ok"):
            raise HTTPException(502, "Couldn't fetch the audio to sync to: " + clip.get("error", "download failed"))
        clipfile = clip["file"]
        cdur = min(_probe_dur(ad / clipfile) or bdur, 60.0)
        det = await asyncio.to_thread(detect_beats, str(ad / clipfile), cdur)
        if not det.get("beats"):
            raise HTTPException(502, "Couldn't find a clear beat in that clip. Try a different link, or use the non-synced generator.")
        pal, acc, mood = await asyncio.to_thread(palette_from, desc, key, model, provider)
        light_ids = [d for d, k in devices.items() if k == "dmx_fixture"]
        fog_ids = [d for d, k in devices.items() if k == "dmx_fog"]
        raw_tracks = compose_beatsync(det, pal, acc, light_ids, fog_ids, cdur, mood)
        bt, _bn, _bb = _san({"tracks": raw_tracks}, devices, cdur, "flat", settings.fog_max_seconds, events_max=400)
        if not bt:
            raise HTTPException(502, "Beat sync produced no events. Try a different clip.")
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
                   tracks={dev: [Event(**e) for e in evs] for dev, evs in bt.items()})
        store.save_scene(sc)
        return {"id": sid, "name": sc.name, "blurb": bblurb, "devices": list(bt.keys()),
                "events": sum(len(v) for v in bt.values()), "tempo": det.get("tempo"),
                "audio": {"file": clipfile, "via": clip.get("via"), "title": clip.get("title")}}

    try:
        tracks, gname, blurb = await asyncio.to_thread(
            _gen, desc, body.duration_s, body.intensity_flow, devices,
            key, model, provider, settings.fog_max_seconds)
    except Exception as e:
        raise HTTPException(502, f"Generation failed: {type(e).__name__}: {str(e)[:200]}")
    if not tracks:
        raise HTTPException(502, "The model returned no usable events. Try again or reword the description.")
    # optional: pull a YouTube clip, auto-pick a segment, attach it as the scene's audio
    audio_obj = None
    audio_note = None
    if body.youtube_url and body.youtube_url.strip():
        from pathlib import Path as _P
        ad = _P(__file__).resolve().parents[2] / settings.audio_dir
        with contextlib.suppress(Exception):
            ad.mkdir(parents=True, exist_ok=True)
        clip = await asyncio.to_thread(youtube_autoclip, body.youtube_url.strip(),
                                       body.duration_s, desc, ad)
        if clip.get("ok"):
            audio_obj = SceneAudio(music=clip["file"], gain=1.0)
            audio_note = {"file": clip["file"], "via": clip.get("via"), "title": clip.get("title")}
            _lib_upsert(clip["file"], title=clip.get("title") or "YouTube clip",
                        category="music", source="youtube", url=body.youtube_url.strip())
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
    sc = Scene(id=sid, name=(body.name or gname or "Generated Scene"),
               duration_s=float(body.duration_s), blurb=blurb, order=order, audio=audio_obj,
               tracks={dev: [Event(**e) for e in evs] for dev, evs in tracks.items()})
    store.save_scene(sc)
    return {"id": sid, "name": sc.name, "blurb": blurb,
            "devices": list(tracks.keys()), "events": sum(len(v) for v in tracks.values()),
            "audio": audio_note}


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
    for dev, st in evaluate(sc, max(0.0, min(float(t), sc.duration_s))).items():
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


def youtube_autoclip(url, target_s, description, d, cap=40.0):
    """Download a YouTube clip and auto-pick a segment to fit the scene: use a
    caption line that matches the description if one exists, else the most intense
    passage. Trims to target_s and cleans up the full download. Not user-editable
    by design; re-generating re-picks. Returns {ok, file, title, via} | {ok:False, error}."""
    import contextlib as _c, glob, os as _os
    dl = _yt_download(url, "music", d)
    if not dl.get("ok"):
        return dl
    src = dl["file"]
    target_s = max(4.0, min(float(target_s), float(cap)))
    start, via = None, "energy"
    caps = _yt_captions(url, d)
    if caps:
        kws = _gen_keywords(description)
        for ts, txt in _parse_vtt(caps):
            if any(k in txt for k in kws):
                start, via = max(0.0, ts - 1.0), "captions"; break
    if start is None:
        start = _energy_start(d / src, target_s)
    tr = _audio_trim(src, start, start + target_s, d)
    with _c.suppress(Exception):
        (d / src).unlink()                       # drop the full download; keep only the clip
    for f in glob.glob(str(d / "cap-*.vtt")):
        with _c.suppress(Exception):
            _os.remove(f)
    if not tr.get("ok"):
        return tr
    return {"ok": True, "file": tr["file"], "title": dl.get("title", ""), "via": via, "dur": tr.get("dur")}


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


@app.post("/api/scenes/{scene_id}/play")
async def play_scene(scene_id: str, opts: PlayOpts | None = None):
    ctrl = _ctrl(app)
    sc = ctrl.store.scenes.get(scene_id)
    if not sc:
        raise HTTPException(404, f"no scene {scene_id}")
    await app.state.player.play(sc, loop=(opts.loop if opts else False), mute=(opts.mute if opts else []))
    return {"playing": scene_id}


class PlayMute(BaseModel):
    mute: list[str] = []


@app.post("/api/play/mute")
async def play_mute(body: PlayMute):
    """Silence/restore lanes on the currently-playing scene, live."""
    app.state.player.set_mute(body.mute)
    return {"ok": True, "mute": body.mute}


class PlaySeek(BaseModel):
    t: float = 0.0


@app.post("/api/play/seek")
async def play_seek(body: PlaySeek):
    """Jump the currently-playing scene to time t."""
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
        for dev, s in evaluate(sc, p._t).items():
            hexc = "#%02x%02x%02x" % s.rgb
            out["devices"][dev] = {"hex": hexc, "brightness": s.brightness,
                                   "strobe": s.strobe_hz > 0, "strobe_hz": round(_cap_hz(s.strobe_hz), 2),
                                   "fog": s.fog}
    bus = getattr(app.state, "bus", None)
    out["overrides"] = bus.manual_devices() if bus is not None else []
    # live presence sensor state for the top signal strip (polled ~3x/sec)
    d = _show(app)
    if d is not None:
        pa = d.presence_active
        out["presence"] = {"active": pa, "connected": pa is not None, "armed": bool(d.armed),
                           "enabled": bool(d.sensor_enabled)}
    return out


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
    ctx = {"settings": settings, "fpp": fpp_info, "vitals": vit,
           "bus_stats": bus.stats() if bus is not None else {},
           "audio_state": _audio_state(), "show_status": _show_status()}
    checks = await asyncio.to_thread(_sys.run_checks, ctx)
    counts = {k: sum(1 for c in checks if c["status"] == k) for k in ("ok", "warn", "fail", "unknown")}
    return {"checks": checks, "counts": counts}


# check id -> (disruptive?, fix). Disruptive fixes restart FPP or this app, so
# they wait until the show is disarmed and no scene is playing.
_FIXES = {
    "fppd":       (True,  lambda: _sys.restart_fppd()),
    "fpp_web":    (False, lambda: _sys.restart_web()),
    "boot_retry": (False, lambda: _sys.fix_boot_retry()),
    "overlay":    (True,  lambda: _sys.fix_overlay(settings)),
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
