"""LLM-backed scene generation from a text description (authoring-time only).

Turns a description + duration + intensity-flow into a validated, SAFE scene.
The LLM proposes the choreography; a hard sanitiser then GUARANTEES the output
can never emit an unsafe strobe or an over-long fog burst, whatever the model
returns. This runs only when the operator clicks Generate in the editor -- never
during the unattended show -- so it carries no night-of reliability risk.

Pure/stdlib + httpx. The sanitiser (sanitize_scene) and the intensity-flow
envelope are dependency-free and unit-tested without any API key.
"""
from __future__ import annotations

import json
import math
import os
import re
import uuid

# --- event vocabulary (must match domain/scenes.py evaluate()) ---
EV_LIGHT = {"set_color", "set_brightness", "blackout", "fade_color",
            "fade_brightness", "pulse", "strobe"}
EV_FOG = {"fog", "blackout"}
SIZABLE = {"fade_color", "fade_brightness", "strobe", "pulse", "fog"}
COLOR_T = {"set_color", "fade_color", "pulse", "strobe"}
BRIGHT_T = {"set_color", "set_brightness", "fade_color", "fade_brightness", "pulse", "strobe"}
CURVE_T = {"fade_color", "fade_brightness"}
CURVES = {"linear", "ease_in", "ease_out", "ease_in_out"}

# --- safety ceilings (independent of whatever the LLM asks for) ---
STROBE_HZ_MAX = 22.0        # existing hand-authored scenes top out ~22 Hz
PULSE_HZ_MAX = 12.0
FOG_BURST_MAX_S = 6.0       # per burst, also clamped to settings.fog_max_seconds
FOG_BURSTS_MAX = 6          # per scene
EVENTS_MAX = 120            # per scene, guards autosave/DOM bloat
FLOOR = 0.15                # intensity-flow never fully blacks the yard

FLOWS = {"flat", "slow_build", "build_strike", "waves", "cold_open"}


def _hexok(c):
    if not isinstance(c, str):
        return None
    c = c.strip()
    if re.match(r"^#?[0-9a-fA-F]{6}$", c):
        return "#" + c.lstrip("#").lower()
    return None


def _f(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def envelope(flow, u):
    """Intensity-flow multiplier in [FLOOR,1] over normalized scene time u in [0,1]."""
    u = max(0.0, min(1.0, u))
    if flow == "slow_build":
        return FLOOR + (1 - FLOOR) * u
    if flow == "build_strike":
        if u < 0.85:
            return FLOOR + (0.7 - FLOOR) * (u / 0.85)
        return 0.7 + 0.3 * ((u - 0.85) / 0.15)
    if flow == "waves":
        return FLOOR + (1 - FLOOR) * (0.5 + 0.5 * math.sin(2 * math.pi * 1.5 * u - math.pi / 2))
    if flow == "cold_open":
        return FLOOR if u < 0.75 else 1.0
    return 1.0  # flat / unknown -> no dimming


def sanitize_scene(raw, devices, duration_s, flow, fog_max_s=8.0, events_max=EVENTS_MAX):
    """Turn a raw LLM dict into a SAFE tracks dict.

    raw:       {"tracks": {dev: [event,...]}, "name"?, "blurb"?}
    devices:   {device_id: kind}  where kind in {"dmx_fixture","dmx_fog"}
    Returns:   (tracks, name, blurb)
    Guarantees: only known device ids; fog only on fog devices; strobe/pulse
    rates capped; fog burst duration and count capped; times/brightness clamped;
    intensity-flow baked into brightness; total events capped.
    """
    dur = max(3.0, _f(duration_s, 30.0))
    flow = flow if flow in FLOWS else "flat"
    fog_cap = min(_f(fog_max_s, 8.0), FOG_BURST_MAX_S)
    src = (raw or {}).get("tracks") or {}
    tracks = {}
    total = 0
    for dev, evs in src.items():
        if dev not in devices or not isinstance(evs, list):
            continue
        is_fog = devices[dev] == "dmx_fog"
        allowed = EV_FOG if is_fog else EV_LIGHT
        out = []
        for e in evs:
            if total >= events_max:
                break
            if not isinstance(e, dict):
                continue
            typ = e.get("type")
            if typ not in allowed:
                continue
            t = max(0.0, min(_f(e.get("t"), 0.0), dur))
            u = (t / dur) if dur else 0.0
            env = envelope(flow, u)
            p = e.get("params") if isinstance(e.get("params"), dict) else {}
            ev = {"id": str(e.get("id") or ("g" + uuid.uuid4().hex[:6])),
                  "t": round(t, 2), "type": typ}
            if typ in SIZABLE:
                d = max(0.1, min(_f(e.get("duration_s"), 1.0), dur - t))
                if typ == "fog":
                    d = min(d, fog_cap)
                ev["duration_s"] = round(d, 2)
            else:
                ev["duration_s"] = 0.0
            np = {}
            if typ in COLOR_T:
                np["color"] = _hexok(p.get("color")) or "#e8a33d"
            if typ in BRIGHT_T:
                b = max(0.0, min(1.0, _f(p.get("brightness"), 1.0))) * env
                np["brightness"] = round(max(0.0, min(1.0, b)), 3)
            if typ in ("pulse", "strobe"):
                cap = STROBE_HZ_MAX if typ == "strobe" else PULSE_HZ_MAX
                np["rate_hz"] = round(max(0.2, min(_f(p.get("rate_hz"), 2.0), cap)), 2)
            if typ == "fog":
                np["intensity"] = round(max(0.0, min(1.0, _f(p.get("intensity"), 0.6))), 3)
            if typ in CURVE_T:
                cv = p.get("curve", "linear")
                np["curve"] = cv if cv in CURVES else "linear"
            ev["params"] = np
            out.append(ev)
            total += 1
        # cap fog bursts on a fog device (keep the earliest N)
        if is_fog:
            fogs = sorted([e for e in out if e["type"] == "fog"], key=lambda e: e["t"])
            if len(fogs) > FOG_BURSTS_MAX:
                keep = {id(e) for e in fogs[:FOG_BURSTS_MAX]}
                out = [e for e in out if e["type"] != "fog" or id(e) in keep]
        out.sort(key=lambda e: e["t"])
        if out:
            tracks[dev] = out
    name = (raw or {}).get("name")
    name = str(name)[:60] if name else None
    blurb = str((raw or {}).get("blurb") or "")[:140]
    return tracks, name, blurb


# --------------------------------------------------------------------------
# Prompting + LLM call
# --------------------------------------------------------------------------
_SYSTEM = (
    "You are a lighting designer for a home Halloween haunt. You compose a light "
    "and fog 'scene' as strict JSON for a small show engine. You never write prose "
    "outside the JSON. You respect the exact event vocabulary and parameter ranges "
    "you are given. You design for dread and contrast: restraint most of the time, "
    "punctuated by a few strong moments, not constant chaos."
)

_EVENT_SPEC = """\
EVENT TYPES and their params (use ONLY these):
- set_color        params: color (#RRGGBB), brightness (0..1)              instant
- set_brightness   params: brightness (0..1)                              instant
- blackout         params: {}                                            instant
- fade_color       params: color, brightness (0..1), curve               has duration_s
- fade_brightness  params: brightness (0..1), curve                      has duration_s
- pulse            params: color, brightness (0..1), rate_hz (0.2..8)     has duration_s
- strobe           params: color, brightness (0..1), rate_hz (2..18)     has duration_s
- fog              params: intensity (0..1)                              has duration_s (<=6s), FOG DEVICES ONLY
curve is one of: linear, ease_in, ease_out, ease_in_out.
Every event needs: t (seconds from scene start), type, and params. Duration events also need duration_s.
"""

_FLOW_HINT = {
    "flat": "Keep intensity roughly steady throughout.",
    "slow_build": "Start subtle and dim, and build steadily brighter/denser toward the end.",
    "build_strike": "Stay restrained for most of the scene, then land one strong hit near the very end.",
    "waves": "Rise and fall in a couple of swells across the scene.",
    "cold_open": "Stay very quiet and dark for the first three quarters, then a sudden bang.",
}


def build_prompt(description, duration_s, flow, devices):
    dur = max(3.0, _f(duration_s, 30.0))
    flow = flow if flow in FLOWS else "flat"
    devlines = "\n".join(
        f'- "{d}"  ({"FOG MACHINE" if k == "dmx_fog" else "RGBW light"})'
        for d, k in devices.items())
    user = f"""\
Design a {dur:.0f} second Halloween scene from this description:
"{description.strip()}"

DEVICES (use these exact ids as the keys of "tracks"; put fog events only on FOG MACHINE devices):
{devlines}

{_EVENT_SPEC}
INTENSITY FLOW: {_FLOW_HINT[flow]}

Compose events across the {dur:.0f}s. Favor a few deliberate moments over wall-to-wall
activity: keep the WHOLE scene to roughly 12-40 events total across all fixtures. Use
fog sparingly (short bursts). Return ONLY this JSON, nothing else:
{{
  "name": "<short evocative scene name>",
  "blurb": "<one short line>",
  "tracks": {{
    "<device_id>": [ {{"t": 0.0, "type": "fade_color", "duration_s": 6.0,
                       "params": {{"color": "#2E0854", "brightness": 0.5, "curve": "ease_in"}}}} ]
  }}
}}"""
    return _SYSTEM, user


def extract_json(text):
    """Pull the first complete {...} object out of an LLM response.

    Handles a ```json``` fence, a prose preamble, and trailing prose by
    brace-matching (respecting strings/escapes). Raises a clear error when the
    object is truncated (model hit its output limit)."""
    if not text or not text.strip():
        raise ValueError("empty LLM response")
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i = t.find("{")
    if i < 0:
        raise ValueError("no JSON object in LLM response")
    depth = 0; instr = False; esc = False
    for j in range(i, len(t)):
        c = t[j]
        if instr:
            esc = (c == "\\" and not esc)
            if c == '"' and not esc:
                instr = False
        elif c == '"':
            instr = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[i:j + 1])
    raise ValueError("JSON object was truncated (model hit its output limit)")


def call_llm(system, user, api_key, model, provider="anthropic", timeout=90):
    import httpx
    if provider == "openai":
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={"model": model, "max_tokens": 8192,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    # default: anthropic
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 8192, "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def resolve_key(settings=None):
    """(api_key, provider, model) from settings or environment. None key => not configured."""
    provider = (getattr(settings, "llm_provider", None) or os.environ.get("HD_LLM_PROVIDER") or "anthropic").lower()
    key = (getattr(settings, "llm_api_key", None) or os.environ.get("HD_LLM_API_KEY")
           or (os.environ.get("OPENAI_API_KEY") if provider == "openai" else os.environ.get("ANTHROPIC_API_KEY")))
    model = (getattr(settings, "llm_model", None) or os.environ.get("HD_LLM_MODEL")
             or ("gpt-4o" if provider == "openai" else "claude-sonnet-5"))
    return key, provider, model


def generate(description, duration_s, flow, devices, api_key, model, provider="anthropic", fog_max_s=8.0):
    """Full pipeline: prompt -> LLM -> parse -> sanitize. Returns (tracks, name, blurb).

    Retries once on a parse failure (model output is stochastic)."""
    system, user = build_prompt(description, duration_s, flow, devices)
    err = None
    for _ in range(2):
        try:
            raw = extract_json(call_llm(system, user, api_key, model, provider))
            return sanitize_scene(raw, devices, duration_s, flow, fog_max_s)
        except ValueError as e:      # parse/truncation only; network errors propagate
            err = e
    raise err


def palette_from(description, api_key, model, provider="anthropic"):
    """Small LLM call -> (palette[list of hex], accent hex, mood). Falls back to
    None/None/'driving' on any failure, so the beat-sync composer uses defaults."""
    system = "You choose a colour palette for a Halloween light scene. Reply ONLY with JSON, no prose."
    user = ('Scene idea: "' + (description or "").strip() + '"\n'
            'Return JSON: {"palette":["#RRGGBB", 3-5 vivid saturated colours that fit the mood], '
            '"accent":"#RRGGBB" (a bright hit colour), "mood":"driving"|"eerie"|"frantic"}.')
    try:
        raw = extract_json(call_llm(system, user, api_key, model, provider))
        pal = [c for c in (raw.get("palette") or []) if _hexok(c)]
        pal = [_hexok(c) for c in pal][:5] or None
        acc = _hexok(raw.get("accent"))
        mood = raw.get("mood") if raw.get("mood") in ("driving", "eerie", "frantic") else "driving"
        return pal, acc, mood
    except Exception:
        return None, None, "driving"
