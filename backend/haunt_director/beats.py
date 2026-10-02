"""Lightweight beat detection + beat-locked scene composition (authoring-time).

No numpy/librosa: ffmpeg decodes the clip, stdlib audioop gives per-hop energy,
and we derive an onset envelope -> autocorrelation tempo -> phase-aligned beat
grid snapped to real onsets. Good enough for steady, beat-driven music (a home
haunt, not a mastering suite). compose_beatsync() lays a light show whose events
land ON those beats.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import uuid

SR = 22050
HOP = 256                       # ~11.6 ms/frame -> fine enough beat timing
_HOP_S = HOP / SR


def _decode(path):
    ff = shutil.which("ffmpeg")
    if not ff:
        return b""
    try:
        r = subprocess.run([ff, "-v", "quiet", "-i", str(path), "-ac", "1",
                            "-ar", str(SR), "-f", "s16le", "-"], capture_output=True, timeout=120)
        return r.stdout
    except Exception:
        return b""


def _onset_env(pcm):
    """Per-hop RMS -> half-wave-rectified energy flux, normalized to [0,1]."""
    import audioop
    step = HOP * 2              # bytes per hop (mono int16)
    n = len(pcm) // step
    if n < 4:
        return []
    rms = [audioop.rms(pcm[i * step:(i + 1) * step], 2) for i in range(n)]
    env = [0.0] * n
    for i in range(1, n):
        d = rms[i] - rms[i - 1]
        env[i] = d if d > 0 else 0.0
    mx = max(env) or 1.0
    return [e / mx for e in env]


def detect_beats(path, max_s=60, min_bpm=75, max_bpm=160):
    """Return {tempo, beat_period, beats:[s], downbeats:[s]}."""
    pcm = _decode(path)
    if not pcm:
        return {"tempo": 0, "beat_period": 0, "beats": [], "downbeats": []}
    max_bytes = int(max_s * SR) * 2
    if len(pcm) > max_bytes:
        pcm = pcm[:max_bytes]
    env = _onset_env(pcm)
    n = len(env)
    if n < 40:
        return {"tempo": 0, "beat_period": 0, "beats": [], "downbeats": []}
    # tempo: autocorrelation of the onset envelope over the plausible lag range
    lag_min = max(2, int(round(60.0 / max_bpm / _HOP_S)))
    lag_max = min(n - 1, int(round(60.0 / min_bpm / _HOP_S)))
    best_lag, best_val = lag_min, -1.0
    for lag in range(lag_min, lag_max + 1):
        s = 0.0
        for i in range(lag, n):
            s += env[i] * env[i - lag]
        # normalize by overlap so long lags aren't penalized
        s /= (n - lag)
        if s > best_val:
            best_val, best_lag = s, lag
    beat_period = best_lag * _HOP_S
    tempo = 60.0 / beat_period if beat_period > 0 else 0.0
    # phase: offset in [0,best_lag) that maximizes onset energy on the grid
    best_phase, best_pv = 0, -1.0
    for ph in range(best_lag):
        s = 0.0; i = ph
        while i < n:
            s += env[i]; i += best_lag
        if s > best_pv:
            best_pv, best_phase = s, ph
    # beat grid, each snapped to the strongest onset in a small window
    win = max(1, best_lag // 6)
    beats = []
    i = best_phase
    while i < n:
        lo, hi = max(0, i - win), min(n - 1, i + win)
        j = max(range(lo, hi + 1), key=lambda k: env[k])
        t = round(j * _HOP_S, 3)
        if not beats or t > beats[-1] + beat_period * 0.5:
            beats.append(t)
        i += best_lag
    # downbeats: whichever of the 4 phases carries the most onset energy
    best_off, best_ov = 0, -1.0
    for off in range(4):
        s = 0.0
        for k in range(off, len(beats), 4):
            idx = int(round(beats[k] / _HOP_S))
            if idx < n:
                s += env[idx]
        if s > best_ov:
            best_ov, best_off = s, off
    downbeats = [beats[k] for k in range(best_off, len(beats), 4)]
    return {"tempo": round(tempo, 1), "beat_period": round(beat_period, 4),
            "beats": beats, "downbeats": downbeats}


# --------------------------------------------------------------------------
# Beat-locked scene composition
# --------------------------------------------------------------------------
_DEF_PALETTE = ["#39ff14", "#8b2fff", "#c8003c", "#2ea6ff"]   # toxic green / violet / blood / cold blue
_DEF_ACCENT = "#ffffff"


def _ev(type_, t, params, dur=0.0):
    e = {"id": "b" + uuid.uuid4().hex[:6], "t": round(float(t), 3), "type": type_, "params": params}
    if dur:
        e["duration_s"] = round(float(dur), 3)
    return e


def compose_beatsync(det, palette, accent, light_ids, fog_ids, duration, mood="driving"):
    """Lay a beat-locked light show. Returns a tracks dict {device_id: [events]}.

    Foundation is a dim palette wash; each beat pops the next fixture to a bright
    palette colour (held to the next beat = a colour-organ on the beat); downbeats
    flash all fixtures to the accent; fog rolls in on the odd bar. Deterministic
    and safety-bounded; the caller still runs the sanitiser."""
    beats = [b for b in (det.get("beats") or []) if b < duration]
    if not beats:
        return {}
    downs = set(round(b, 3) for b in (det.get("downbeats") or []))
    pal = [c for c in (palette or []) if isinstance(c, str)] or _DEF_PALETTE
    acc = accent if isinstance(accent, str) else _DEF_ACCENT
    lights = list(light_ids)
    tracks = {d: [] for d in lights}
    # base wash so the yard is never fully dark between hits
    for idx, d in enumerate(lights):
        tracks[d].append(_ev("fade_color", 0.0, {"color": pal[idx % len(pal)], "brightness": 0.22, "curve": "ease_in"}, dur=1.2))
    gentle = (mood == "eerie")
    hi = 0.8 if gentle else 0.98
    for i, b in enumerate(beats):
        nxt = beats[i + 1] if i + 1 < len(beats) else duration
        hold = max(0.08, (nxt - b) * 0.92)
        if round(b, 3) in downs:                       # the "hit": all fixtures pop to accent
            for d in lights:
                tracks[d].append(_ev("set_color", b, {"color": acc, "brightness": 1.0}))
                tracks[d].append(_ev("fade_brightness", b + 0.02, {"brightness": 0.2, "curve": "ease_out"}, dur=hold))
        else:                                          # groove: rotate one fixture to a palette colour
            d = lights[i % len(lights)]
            tracks[d].append(_ev("set_color", b, {"color": pal[i % len(pal)], "brightness": hi}))
            tracks[d].append(_ev("fade_brightness", b + 0.02, {"brightness": 0.16, "curve": "ease_out"}, dur=hold))
    # fog: a short burst at the start of every 4th bar (16 beats), not gentle mood
    if fog_ids and not gentle:
        fd = fog_ids[0]
        tracks[fd] = []
        for k in range(0, len(beats), 16):
            bt = beats[k]
            if bt < duration - 1:
                tracks[fd].append(_ev("fog", bt, {"intensity": 0.7}, dur=min(4.0, duration - bt)))
    return {d: evs for d, evs in tracks.items() if evs}
