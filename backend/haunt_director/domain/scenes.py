"""Scene model + timeline evaluation (see docs/how-it-works.md).

A scene is per-device tracks of timed events. evaluate(scene, t) returns each
device's live state at time t, which the player turns into DMX writes. Pure, no I/O.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

SIZABLE = {"fade_color", "fade_brightness", "strobe", "pulse", "fog"}
INSTANT = {"set_color", "set_brightness", "blackout"}
# A strobe's active window is floored to this so it always lasts long enough to
# actually flash on the hardware. A sub-~0.3s strobe can land between the
# fixture's flashes (which run near 8Hz) and, because playback starts on a
# slightly different frame each time, fire only intermittently. Floored here and
# capped to the next event so it never overruns the following beat.
MIN_STROBE_S = 0.4


class Event(BaseModel):
    id: str
    t: float                       # start time, seconds
    type: str
    duration_s: float = 0.0
    params: dict = Field(default_factory=dict)


class AudioCue(BaseModel):
    id: str
    file: str                          # filename in web/audio/
    at_s: float = 0.0                  # when it starts
    gain: float = 1.0                  # 0..1
    loop: bool = False                 # loop (ambient bed) vs one-shot


class SceneAudio(BaseModel):
    music: str | None = None          # background score filename (in web/audio/)
    voiceover: str | None = None      # spoken line filename
    voiceover_at_s: float = 0.0       # when the voice line starts
    gain: float = 1.0                 # 0..1 music volume
    layers: list[AudioCue] = Field(default_factory=list)  # layered beds / sfx cues


class Scene(BaseModel):
    schema_version: int = 1
    id: str
    name: str
    duration_s: float = 12.0
    blurb: str = ""
    audio: SceneAudio | None = None
    audio_fade_out_s: float | None = None   # crossfade tail length; None = use global default
    enabled: bool = True           # include in the show / editor toggle
    order: int = 0                 # display order in the scene list
    tag: str = ""                  # show role: "ambient" | "scare" | "" (manual only)
    tracks: dict[str, list[Event]] = Field(default_factory=dict)


class DeviceState(BaseModel):
    rgb: tuple[int, int, int] = (0, 0, 0)
    brightness: int = 0            # 0-255
    strobe_hz: float = 0.0
    fog: int = 0                   # 0-255 (fog devices)


def _hex(h: str) -> tuple[int, int, int]:
    h = (h or "#000000").lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, f))


def _ease(f: float, curve: str) -> float:
    f = max(0.0, min(1.0, f))
    if curve == "ease_in":
        return f * f
    if curve == "ease_out":
        return 1 - (1 - f) * (1 - f)
    if curve == "ease_in_out":
        return 3 * f * f - 2 * f * f * f
    return f  # linear


def _evaluate_track(events: list[Event], t: float) -> DeviceState:
    """Replay a device's events up to time t, interpolating an active fade.

    A timed fade (fade_color/fade_brightness) is capped to finish before the
    next event on the same track begins. Without this a fade whose duration
    overruns the next event never reaches its target — it's still mid-transition
    (often still on the previous colour) when that event fires, so e.g. a fade to
    amber that follows a blue reads as bluish. The cap only shortens fades that
    would otherwise overrun; fades that already fit are untouched. Both the live
    rig and the editor's simulator read this same evaluation, so they stay in
    sync."""
    rgb = (0, 0, 0)
    bri = 0.0            # 0..1 internally
    strobe = 0.0
    fog = 0.0
    evs = sorted(events, key=lambda x: x.t)
    for idx, e in enumerate(evs):
        p = e.params
        if e.t > t and e.type in INSTANT:
            continue
        active = e.t <= t <= e.t + max(e.duration_s, 0)
        done = t > e.t + max(e.duration_s, 0)
        # fade length, capped so the fade lands before the next event begins
        fdur = max(e.duration_s, 0.0)
        if idx + 1 < len(evs):
            gap = evs[idx + 1].t - e.t
            if gap > 0:
                fdur = min(fdur, gap)

        if e.type == "set_color":
            rgb = _hex(p.get("color")); bri = float(p.get("brightness", 1.0))
        elif e.type == "set_brightness":
            bri = float(p.get("brightness", 1.0))
        elif e.type == "blackout":
            rgb = (0, 0, 0); bri = 0.0; fog = 0.0; strobe = 0.0
        elif e.type == "fade_color":
            f = _ease((t - e.t) / fdur if fdur else 1.0, p.get("curve", "linear"))
            tgt = _hex(p.get("color")); tb = float(p.get("brightness", 1.0))
            rgb = tuple(int(_lerp(rgb[k], tgt[k], f)) for k in range(3))
            bri = _lerp(bri, tb, f)
        elif e.type == "fade_brightness":
            f = _ease((t - e.t) / fdur if fdur else 1.0, p.get("curve", "linear"))
            bri = _lerp(bri, float(p.get("brightness", 0.0)), f)
        elif e.type == "pulse":
            if active:
                import math
                hz = float(p.get("rate_hz", 1.0)); base = float(p.get("brightness", 1.0))
                bri = base * (0.5 + 0.5 * math.sin(2 * math.pi * hz * (t - e.t)))
                if p.get("color"): rgb = _hex(p["color"])
        elif e.type == "strobe":
            sdur = max(max(e.duration_s, 0.0), MIN_STROBE_S)
            if idx + 1 < len(evs):
                sgap = evs[idx + 1].t - e.t
                if sgap > 0:
                    sdur = min(sdur, sgap)
            s_active = e.t <= t <= e.t + sdur
            s_done = t > e.t + sdur
            strobe = float(p.get("rate_hz", 10)) if s_active else (0.0 if s_done else strobe)
            if s_active and p.get("color"): rgb = _hex(p["color"]); bri = float(p.get("brightness", 1.0))
        elif e.type == "fog":
            fog = float(p.get("intensity", 0.6)) if active else (0.0 if done else fog)

    return DeviceState(
        rgb=rgb, brightness=int(max(0, min(255, bri * 255))),
        strobe_hz=strobe, fog=int(max(0, min(255, fog * 255))))


def evaluate(scene: Scene, t: float) -> dict[str, DeviceState]:
    return {dev: _evaluate_track(evs, t) for dev, evs in scene.tracks.items()}
