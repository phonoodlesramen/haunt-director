"""Scene player: owns the scene LIFECYCLE and its AUDIO. The output bus
(output.py) owns the lights.

Lights. play() hands the bus a scene *source*: a function the bus's own thread
calls every frame to evaluate the scene at a shared clock. Light timing no longer
depends on this asyncio loop at all -- web requests, audio work and GC pauses on
the loop cannot delay or drop a frame.

Audio. Cue timing (voice-over, layered cues, loop restarts, seek) runs here on
the asyncio loop, reading the same clock. Every sound a scene needs is loaded in
a worker thread BEFORE the handoff (pygame releases the GIL while loading --
verified on the Pi), so no file I/O blocks the loop mid-show.

Exactly one scene runs at a time. play()/stop() switch under a lock, and a
generation counter makes the newest request win: a play() still preloading when
a newer play() or a stop() arrives simply gives up. (Before this, two overlapping
play() calls could each start a writer task; the loser was never cancelled and
kept writing the rig, even through Stop.)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .domain.devices import DeviceKind
from .domain.profiles import ChannelRole
from .domain.resolve import resolve_fixture
from .domain.scenes import Scene, evaluate, DeviceState

log = logging.getLogger("haunt.player")

FPS = 25              # scene-engine rate (kept for callers; the bus owns the clock)
CUE_TICK_S = 0.02     # audio-cue scheduling resolution
DUCK_DEFAULT_DB = 9   # voice ducking: clearly under the voice, still present
DUCK_MAX_DB = 18


def scene_channels(store, scene: Scene, t: float, mute=()) -> dict[int, int]:
    """A scene's absolute DMX channel values at time t. Pure: safe to call from
    the output thread. (The fog guard is applied by the bus, not here.)"""
    states = evaluate(scene, t)
    channels: dict[int, int] = {}
    for dev_id, st in states.items():
        dev = store.device(dev_id)
        if dev is None or not dev.enabled or dev.dmx_address is None:
            continue
        if dev_id in mute:
            st = DeviceState()                    # muted in the editor: force dark
        prof = store.profile_for(dev)
        if prof is None:
            continue
        if dev.kind == DeviceKind.DMX_FOG:
            off = prof.offset_for(ChannelRole.FOG)
            if off is not None:
                channels[dev.dmx_address + off] = st.fog
        else:
            channels.update(resolve_fixture(
                prof, dev.dmx_address, rgb=st.rgb,
                brightness=st.brightness, strobe_hz=st.strobe_hz))
    return channels


def scene_audio_files(scene: Scene) -> list[str]:
    au = getattr(scene, "audio", None)
    if not au:
        return []
    files = [au.music, au.voiceover] + [c.file for c in (au.layers or [])]
    return [f for f in dict.fromkeys(files) if f]


class _Run:
    """One playback of one scene: the clock the bus and the cue task share.
    `start` and `mute` are plain attribute swaps, safe to read from the bus."""

    __slots__ = ("scene", "loop", "mute", "start", "sustain", "roles")

    def __init__(self, scene: Scene, loop: bool, mute: set, sustain: bool, roles=None):
        self.scene = scene
        self.roles = dict(roles or {})            # layer cue id -> "voice" | "bed" | "sfx"
        self.loop = bool(loop)
        self.mute = set(mute)
        self.sustain = bool(sustain)
        self.start = time.monotonic()

    def raw(self, now: float) -> float:
        return max(0.0, now - self.start)

    def t(self, now: float) -> float:
        d = self.scene.duration_s
        r = self.raw(now)
        if self.loop and d > 0:
            return r % d
        return min(r, d)


class ShowPlayer:
    def __init__(self, ctrl, bus, audio=None):
        self.ctrl = ctrl
        self.bus = bus
        self.audio = audio
        self._lock = asyncio.Lock()
        self._gen = 0
        self._task: asyncio.Task | None = None
        self._run: _Run | None = None
        self._seek_pending: float | None = None
        # ---- audio crossfade between scenes ----
        self.default_crossfade_s: float = 2.0     # global default (set from show config)
        self._suppress_audio_stop: bool = False   # a transition is handing the audio off
        self._audio_owner: Scene | None = None     # scene whose audio is sounding now
        self._last_scene_id: str | None = None     # the scene most recently started
        # ---- global output guards (persisted in data/output.json) ----
        self.audio_muted: bool = False            # True -> no music, voice-over, or cues
        self.volume: int = 100                    # app master volume, 0-100 (slider %)
        self.duck_db: int = DUCK_DEFAULT_DB       # voice ducking depth, dB (0 = off)
        # files -> the subset that are spoken lines (the app wires this to the
        # audio library's categories, the same ones the editor's VOICE lanes use)
        self.voice_files = None

    # ---- the fog guard lives on the bus (it must cover manual fog too) ----
    @property
    def fog_enabled(self) -> bool:
        return self.bus.fog_enabled

    @fog_enabled.setter
    def fog_enabled(self, v: bool) -> None:
        self.bus.fog_enabled = bool(v)

    # ---- master volume ----
    @property
    def volume_gain(self) -> float:
        """Slider % -> amplitude on a square-law (perceptual) curve, so the
        slider feels even: 50% = -12 dB, 75% = -5 dB, 100% = unity."""
        return (max(0, min(100, int(self.volume))) / 100.0) ** 2

    def set_volume(self, pct) -> None:
        self.volume = max(0, min(100, int(round(float(pct)))))
        if self.audio is not None:
            with contextlib.suppress(Exception):
                self.audio.set_master(self.volume_gain)

    # ---- voice ducking ----
    def set_duck(self, db) -> None:
        """Music + looping beds dip this many dB while a voice plays (0 = off)."""
        try:
            v = int(round(float(db)))
        except Exception:
            v = DUCK_DEFAULT_DB
        self.duck_db = max(0, min(DUCK_MAX_DB, v))
        if self.audio is not None:
            with contextlib.suppress(Exception):
                self.audio.set_duck_db(self.duck_db)

    def _layer_roles(self, scene: Scene) -> dict:
        """cue id -> "voice" (ducks the bed) | "bed" (looping, is ducked) | "sfx"
        (one-shot, left alone). Reads the audio library, so call off the loop."""
        au = getattr(scene, "audio", None)
        layers = list(getattr(au, "layers", None) or [])
        if not layers:
            return {}
        voices = set()
        if callable(self.voice_files):
            with contextlib.suppress(Exception):
                voices = set(self.voice_files([c.file for c in layers]) or ())
        return {c.id: ("voice" if c.file in voices else ("bed" if c.loop else "sfx"))
                for c in layers}

    # ---- status ----
    @property
    def is_playing(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def _t(self) -> float:
        r = self._run
        return r.t(time.monotonic()) if r is not None else 0.0

    @property
    def _scene_id(self) -> str | None:
        r = self._run
        return r.scene.id if r is not None else None

    @property
    def status(self) -> dict:
        r = self._run
        return {"playing": self.is_playing, "scene": self._scene_id,
                "t": round(self._t, 2), "loop": bool(r and r.loop)}

    # ---- live controls ----
    def set_mute(self, mute) -> None:
        """Silence/restore lanes on the playing scene, live (lights + audio)."""
        m = set(mute or [])
        r = self._run
        if r is not None:
            r.mute = m
        if self.audio is not None:
            with contextlib.suppress(Exception):
                self.audio.set_mute(m)

    def seek(self, t) -> None:
        """Jump the playing scene to time t. Lights follow on the next frame;
        the cue task re-syncs the audio."""
        r = self._run
        if not self.is_playing or r is None:
            return
        tgt = max(0.0, min(float(t), r.scene.duration_s))
        r.start = time.monotonic() - tgt
        self._seek_pending = tgt

    def _resolve_crossfade(self, override) -> float:
        """Crossfade length (s) for handing off from the scene whose audio is
        sounding: explicit override, else that scene's value, else the default."""
        if self.audio is None or self._audio_owner is None:
            return 0.0
        if override is not None:
            return max(0.0, float(override))
        v = getattr(self._audio_owner, "audio_fade_out_s", None)
        if v is not None:
            return max(0.0, float(v))
        return max(0.0, float(self.default_crossfade_s))

    # ---- lifecycle ----
    async def play(self, scene: Scene, loop: bool = False, mute=None,
                   sustain_on_end: bool = False, crossfade=None) -> None:
        self._gen += 1
        my = self._gen
        # Load every sound this scene needs OFF the event loop while the current
        # scene keeps running, so the switch below is instant.
        roles = {}
        if self.audio is not None:
            files = scene_audio_files(scene)
            if files:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.audio.preload, files)
            with contextlib.suppress(Exception):
                roles = await asyncio.to_thread(self._layer_roles, scene)
        async with self._lock:
            if my != self._gen:
                return                            # superseded by a newer play()/stop()
            xf = self._resolve_crossfade(crossfade)
            # lights are NOT touched here: the bus keeps rendering the outgoing
            # scene until set_scene() below swaps in the new one -- no dark gap.
            await self._end_current(xf, lights_off=False)
            run = _Run(scene, loop, set(mute or []), sustain_on_end, roles)
            au = getattr(scene, "audio", None)
            self._audio_owner = scene if (au is not None and self.audio is not None) else None
            if au and self.audio is not None:
                with contextlib.suppress(Exception):
                    self.audio.set_mute(run.mute)
                    if au.music and not self.audio_muted:
                        self.audio.play_music(au.music, gain=au.gain, loop=True)
            run.start = time.monotonic()
            self._run = run
            self._last_scene_id = scene.id
            self._seek_pending = None
            store = self.ctrl.store
            self.bus.set_scene(lambda now, r=run: scene_channels(store, r.scene, r.t(now), r.mute))
            self._task = asyncio.create_task(self._cues(run), name=f"cues:{scene.id}")

    async def stop(self) -> None:
        """Hard stop: cut audio and drop the scene layer (manual overrides stay;
        STOP EVERYTHING clears those too, via ControlService.stop_everything)."""
        self._gen += 1
        async with self._lock:
            await self._end_current(0.0, lights_off=True)
            self._last_scene_id = None

    async def settle(self, scene_id: str | None) -> bool:
        """Wind down scene `scene_id` if it is still the last thing started:
        its audio fades out over the crossfade time and the rig goes dark
        (held frame dropped). Used when a show has nothing to hand off to, e.g.
        after a scare in a sensor-only show. Anything started after it (a
        scene played by hand) is left alone. Returns True if it acted."""
        if scene_id is None:
            return False
        async with self._lock:
            if self._last_scene_id != scene_id:
                return False
            await self._end_current(self._resolve_crossfade(None), lights_off=True)
            self._last_scene_id = None
            return True

    async def _end_current(self, fade_s: float, lights_off: bool) -> None:
        task = self._task
        if task is not None and not task.done():
            if fade_s > 0 and self.audio is not None:
                self._suppress_audio_stop = True   # the task's finally leaves audio running
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        self._run = None
        self._seek_pending = None
        self._suppress_audio_stop = False
        if self.audio is not None:
            if fade_s > 0:
                with contextlib.suppress(Exception):
                    self.audio.fade_out_current(int(fade_s * 1000))
            else:
                with contextlib.suppress(Exception):
                    self.audio.stop()
        if lights_off:
            self.bus.end_scene(hold=False)
        self._audio_owner = None

    async def _cues(self, run: _Run) -> None:
        """Audio cue scheduling + end-of-scene detection for one run."""
        au = getattr(run.scene, "audio", None)
        vo_fired = False
        fired: set = set()
        lap = 0
        ended = False
        try:
            while True:
                sp = self._seek_pending
                if sp is not None and self._run is run:
                    self._seek_pending = None
                    vo_fired = bool(au and au.voiceover and au.voiceover_at_s <= sp)
                    fired = set()
                    d = run.scene.duration_s
                    lap = int(run.raw(time.monotonic()) // d) if (run.loop and d > 0) else 0
                    if au and self.audio is not None:
                        with contextlib.suppress(Exception):
                            self.audio.stop()
                            self.audio.set_mute(run.mute)
                            if au.music and not self.audio_muted:
                                self.audio.play_music(au.music, gain=au.gain, loop=True)
                            for cue in (au.layers or []):
                                if cue.at_s <= sp:
                                    fired.add(cue.id)
                                    if cue.loop and not self.audio_muted:
                                        self.audio.play_cue(cue.file, gain=cue.gain,
                                                            loop=cue.loop, key=cue.id,
                                                            role=run.roles.get(cue.id))
                raw = run.raw(time.monotonic())
                d = run.scene.duration_s
                if run.loop and d > 0:
                    cur = int(raw // d)
                    if cur != lap:                 # wrapped: re-arm this lap's cues
                        lap = cur
                        vo_fired = False
                        fired = set()
                    t = raw - cur * d
                else:
                    if raw >= d:
                        ended = True
                        break
                    t = raw
                if au and self.audio is not None:
                    if au.voiceover and not vo_fired and t >= au.voiceover_at_s:
                        vo_fired = True
                        if not self.audio_muted:
                            with contextlib.suppress(Exception):
                                self.audio.play_voiceover(au.voiceover, gain=au.gain)
                    for cue in (au.layers or []):
                        if cue.id not in fired and t >= cue.at_s:
                            fired.add(cue.id)
                            if not self.audio_muted:
                                with contextlib.suppress(Exception):
                                    self.audio.play_cue(cue.file, gain=cue.gain,
                                                        loop=cue.loop, key=cue.id,
                                                        role=run.roles.get(cue.id))
                await asyncio.sleep(CUE_TICK_S)
        finally:
            if self._run is run:                   # never clobber a newer run's state
                if ended:
                    # natural end: the rig holds the final frame (fog + strobe off)
                    # until the next scene swaps in -- same look as before, safer.
                    self.bus.end_scene(hold=True)
                stop_audio = not (self._suppress_audio_stop or run.sustain)
                if self.audio is not None and stop_audio:
                    with contextlib.suppress(Exception):
                        self.audio.stop()
                    self._audio_owner = None
                self._run = None
