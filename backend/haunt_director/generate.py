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
FOG_BURSTS_MAX = 6          # per scene (scenes up to 3 min); longer scenes get 1 per 30 s
FOG_GAP_S = 10.0            # clear air after a burst before the next may start
STROBE_MAX_S = 8.0          # longest single strobe a generated scene may hold
STROBE_GAP_S = 2.0          # rest after a strobe before the next may start
EVENTS_MAX = 120            # per scene floor, guards autosave/DOM bloat (scales with length)
FLOOR = 0.15                # intensity-flow never fully blacks the yard

# --- long scenes ---
MAX_SCENE_S = 300.0         # longest scene the generator (and Extend) will make
SECTION_S = 60.0            # longer scenes are written in parts of at most this long


def event_budget(length_s):
    """(low, high) number of events to ask for across all fixtures for a stretch
    this long: about one deliberate change every 1-2.5 s, never fewer than 8."""
    L = max(1.0, float(length_s))
    return max(8, int(round(L * 0.4))), max(24, int(round(L * 1.2)))


def fog_bursts_max(duration_s):
    """Fog bursts allowed in a whole scene: 6, or 1 per 30 s for long scenes."""
    return max(FOG_BURSTS_MAX, int(math.ceil(max(0.0, float(duration_s)) / 30.0)))


def events_cap(duration_s):
    """Hard ceiling on a generated scene's events (well above what we ask for)."""
    return max(EVENTS_MAX, int(float(duration_s) * 1.5))


def sections_for(duration_s, size=SECTION_S):
    """Split a scene into equal parts of at most `size` seconds: [(start, end), ...]."""
    dur = max(1.0, float(duration_s))
    n = max(1, int(math.ceil(dur / size - 1e-9)))
    step = dur / n
    return [(round(i * step, 2), round(dur if i == n - 1 else (i + 1) * step, 2)) for i in range(n)]

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


def _spread(items, k):
    """k items picked evenly across the list (first and last included)."""
    n = len(items)
    if k <= 0:
        return []
    if n <= k:
        return list(items)
    if k == 1:
        return [items[0]]
    idx = sorted({int(round(i * (n - 1) / (k - 1))) for i in range(k)})
    return [items[i] for i in idx]


def _spaced(evs, typ, gap):
    """Drop any `typ` event that starts less than `gap` s after the previous
    kept one ends (so bursts can't run together into one long one)."""
    out, last_end = [], None
    for e in evs:
        if e["type"] == typ:
            if last_end is not None and e["t"] < last_end + gap:
                continue
            last_end = e["t"] + e["duration_s"]
        out.append(e)
    return out


def sanitize_scene(raw, devices, duration_s, flow, fog_max_s=8.0, events_max=None,
                   fog_bursts=None, window=None):
    """Turn a raw LLM dict into a SAFE tracks dict.

    raw:       {"tracks": {dev: [event,...]}, "name"?, "blurb"?}
    devices:   {device_id: kind}  where kind in {"dmx_fixture","dmx_fog"}
    Returns:   (tracks, name, blurb)
    Guarantees: only known device ids; fog only on fog devices; strobe/pulse
    rates capped; every fog burst <= FOG_BURST_MAX_S with >= FOG_GAP_S of clear
    air before the next, and the burst count capped (spread across the scene);
    every strobe <= STROBE_MAX_S with a STROBE_GAP_S rest after it; events past
    the end dropped; brightness clamped with the intensity flow baked in; total
    events capped fairly across fixtures; every event gets a fresh unique id.

    window:    optional (start, end): keep only events that start inside it and
               end them by `end` (used to fill one part of a longer scene).
    """
    dur = max(3.0, _f(duration_s, 30.0))
    events_max = events_cap(dur) if events_max is None else int(events_max)
    fog_bursts = fog_bursts_max(dur) if fog_bursts is None else max(0, int(fog_bursts))
    w_lo, w_hi = (0.0, dur) if window is None else (max(0.0, float(window[0])), min(dur, float(window[1])))
    flow = flow if flow in FLOWS else "flat"
    fog_cap = min(_f(fog_max_s, 8.0), FOG_BURST_MAX_S)
    src = (raw or {}).get("tracks") or {}
    tracks = {}
    for dev, evs in src.items():
        if dev not in devices or not isinstance(evs, list):
            continue
        is_fog = devices[dev] == "dmx_fog"
        allowed = EV_FOG if is_fog else EV_LIGHT
        out = []
        for e in evs:
            if not isinstance(e, dict):
                continue
            typ = e.get("type")
            if typ not in allowed:
                continue
            t = _f(e.get("t"), 0.0)
            if window is not None and not (w_lo <= t < w_hi):
                continue
            if t > dur:
                continue                          # past the end: drop, don't pile up at the end
            t = max(0.0, t)
            u = (t / dur) if dur else 0.0
            env = envelope(flow, u)
            p = e.get("params") if isinstance(e.get("params"), dict) else {}
            ev = {"id": "g" + uuid.uuid4().hex[:8], "t": round(t, 2), "type": typ}
            if typ in SIZABLE:
                d = max(0.1, min(_f(e.get("duration_s"), 1.0), w_hi - t))
                if typ == "fog":
                    d = min(d, fog_cap)
                elif typ == "strobe":
                    d = min(d, STROBE_MAX_S)
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
        out.sort(key=lambda e: e["t"])
        if is_fog:
            # never back-to-back: clear air between bursts, then a capped count
            # spread across the scene (not bunched at the start)
            out = _spaced(out, "fog", FOG_GAP_S)
            fogs = [e for e in out if e["type"] == "fog"]
            if len(fogs) > fog_bursts:
                keep = {id(e) for e in _spread(fogs, fog_bursts)}
                out = [e for e in out if e["type"] != "fog" or id(e) in keep]
        else:
            out = _spaced(out, "strobe", STROBE_GAP_S)
        if out:
            tracks[dev] = out
    # event ceiling, shared fairly: every fixture keeps the same share of its
    # own events, so no fixture is starved because of dict order. The ones kept
    # are spread across the scene (first and last included), so thinning makes
    # a busy scene sparser everywhere instead of cutting off its ending.
    total = sum(len(v) for v in tracks.values())
    if total > events_max:
        for dev in list(tracks):
            n = len(tracks[dev])
            tracks[dev] = _spread(tracks[dev], max(1, (events_max * n) // total))
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
    devlines = _devlines(devices)
    lo, hi = event_budget(dur)
    user = f"""\
Design a {dur:.0f} second Halloween scene from this description:
"{description.strip()}"

DEVICES (use these exact ids as the keys of "tracks"; put fog events only on FOG MACHINE devices):
{devlines}

{_EVENT_SPEC}
INTENSITY FLOW: {_FLOW_HINT[flow]}

Compose events across the {dur:.0f}s. Favor a few deliberate moments over wall-to-wall
activity: keep the WHOLE scene to roughly {lo}-{hi} events total across all fixtures. Use
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


def _devlines(devices):
    return "\n".join(
        f'- "{d}"  ({"FOG MACHINE" if k == "dmx_fog" else "RGBW light"})'
        for d, k in devices.items())


_TRACKS_JSON = """{
  "tracks": {
    "<device_id>": [ {"t": <seconds>, "type": "fade_color", "duration_s": 6.0,
                       "params": {"color": "#2E0854", "brightness": 0.5, "curve": "ease_in"}} ]
  }
}"""


def _energy_word(v):
    return "low" if v < 0.4 else ("medium" if v < 0.75 else "high")


def _flow_span(flow, start, end, dur):
    """Where this part sits in the scene's energy curve, in words. Deliberately
    NOT a brightness: the sanitizer bakes the flow's dimming in afterwards, so
    a brightness here would be applied twice and leave long scenes near black."""
    a = envelope(flow, start / dur if dur else 0.0)
    b = envelope(flow, end / dur if dur else 1.0)
    wa, wb = _energy_word(a), _energy_word(b)
    shape = (f"{wa} energy through this part" if wa == wb
             else f"{wa} energy at the start of this part, {wb} by its end")
    return (shape + " (energy = how much happens and how hard it hits; the engine applies the "
            "overall dimming curve itself, so write brightness on the full 0-1 scale)")


def build_outline_prompt(description, duration_s, flow, sections):
    """First call for a long scene: a name, a blurb and a story beat per part."""
    dur = max(3.0, _f(duration_s, 60.0))
    flow = flow if flow in FLOWS else "flat"
    parts = "\n".join(f"- Part {i + 1}: {a:.0f} to {b:.0f} s" for i, (a, b) in enumerate(sections))
    user = f"""\
Plan a {dur:.0f} second Halloween light-and-fog scene from this description:
"{description.strip()}"

INTENSITY FLOW: {_FLOW_HINT[flow]}

It will be written in {len(sections)} parts:
{parts}

Give the whole scene a story arc across the parts (an opening, a middle that develops,
and an ending), not the same idea repeated. For each part, say in one or two sentences what
happens, and the look it opens on and closes on (colours as #RRGGBB, and roughly how bright).
Each part opens on the look the part before it closes on.
Return ONLY this JSON, nothing else:
{{
  "name": "<short evocative scene name>",
  "blurb": "<one short line>",
  "parts": [ {{"story": "...", "opens": "...", "closes": "..."}} ]
}}"""
    return _SYSTEM, user


def build_section_prompt(description, duration_s, flow, devices, sections, k, outline):
    """One part of a long scene, in absolute scene time."""
    dur = max(3.0, _f(duration_s, 60.0))
    flow = flow if flow in FLOWS else "flat"
    a, b = sections[k]
    parts = outline.get("parts") if isinstance(outline, dict) else None
    parts = [x if isinstance(x, dict) else {} for x in parts] if isinstance(parts, list) else []
    arc = "\n".join(
        f"{i + 1}. ({sa:.0f}-{sb:.0f} s) " + (str((parts[i] if i < len(parts) else {}).get("story") or "").strip()
                                               or "(continue the scene)")
        for i, (sa, sb) in enumerate(sections))
    me = parts[k] if k < len(parts) else {}
    lo, hi = event_budget(b - a)
    fog_part = max(1, int(math.ceil(fog_bursts_max(dur) / len(sections))))
    opens = str(me.get("opens") or "").strip()
    closes = str(me.get("closes") or "").strip()
    looks = ""
    if opens or closes:
        looks = f"\nIt opens on: {opens or 'whatever the previous part closed on'}. It closes on: {closes or 'a look the next part can pick up from'}."
    user = f"""\
You are writing part {k + 1} of {len(sections)} of a {dur:.0f} second Halloween scene.

WHOLE-SCENE IDEA: "{description.strip()}"
STORY ACROSS THE PARTS:
{arc}

THIS PART: part {k + 1}, from t={a:.1f} s to t={b:.1f} s. {str(me.get("story") or "").strip()}{looks}
ENERGY: {_flow_span(flow, a, b, dur)}.

DEVICES (use these exact ids as the keys of "tracks"; put fog events only on FOG MACHINE devices):
{_devlines(devices)}

{_EVENT_SPEC}
Use ABSOLUTE scene times: every t must be between {a:.1f} and {b:.1f}. Put roughly {lo}-{hi}
events in this part across all fixtures, favouring a few deliberate moments over constant
activity. At most {fog_part} fog burst(s) in this part. Return ONLY this JSON, nothing else:
{_TRACKS_JSON}"""
    return _SYSTEM, user


def build_extend_prompt(ctx):
    """Continue an existing scene: ctx has name, blurb, description, note, start,
    end, devices, end_state {dev: "#rrggbb at NN%"}, recent {dev: [line,...]},
    palette [hex], fog_left."""
    a, b = float(ctx["start"]), float(ctx["end"])
    lo, hi = event_budget(b - a)
    ends = "\n".join(f'- "{d}": {v}' for d, v in (ctx.get("end_state") or {}).items()) or "- (dark)"
    recent = "\n".join(f'- "{d}": ' + "; ".join(lines) for d, lines in (ctx.get("recent") or {}).items() if lines) \
        or "- (nothing in the last stretch)"
    pal = ", ".join(ctx.get("palette") or []) or "your choice"
    note = (ctx.get("note") or "").strip()
    fog_left = int(ctx.get("fog_left", 0))
    fog_line = (f"At most {fog_left} fog burst(s) in the new part." if fog_left > 0
                else "Do not use any fog in the new part.")
    user = f"""\
Continue an existing {a:.0f} second Halloween scene for {b - a:.0f} more seconds.

SCENE: "{ctx.get('name') or 'Scene'}" ({ctx.get('blurb') or 'no description'})
{('ORIGINAL IDEA: "' + ctx['description'].strip() + '"') if ctx.get('description') else ''}
{('WHAT HAPPENS NEXT: "' + note + '"') if note else 'WHAT HAPPENS NEXT: keep the same mood and develop it, then land on a satisfying close at the very end.'}

WHERE EACH FIXTURE IS AT t={a:.1f} s:
{ends}
THE LAST STRETCH OF THE SCENE (time, event, colour, brightness):
{recent}
COLOURS THE SCENE USES: {pal}

DEVICES (use these exact ids as the keys of "tracks"; put fog events only on FOG MACHINE devices):
{_devlines(ctx["devices"])}

{_EVENT_SPEC}
Pick up smoothly from where each fixture is. Use ABSOLUTE scene times: every t must be
between {a:.1f} and {b:.1f}. Put roughly {lo}-{hi} events in the new part across all fixtures.
{fog_line} Return ONLY this JSON, nothing else:
{_TRACKS_JSON}"""
    return _SYSTEM, user


def normalize_part_times(raw, start, end):
    """A model sometimes counts a part from 0 instead of from its real start.
    If more of its events fit [0, length) than [start, end), shift them all by start."""
    tracks = (raw or {}).get("tracks") or {}
    ts = [_f(e.get("t"), -1.0) for evs in tracks.values() if isinstance(evs, list)
          for e in evs if isinstance(e, dict)]
    if not ts or start <= 0:
        return raw
    length = end - start
    absolute = sum(1 for t in ts if start <= t < end)
    relative = sum(1 for t in ts if 0 <= t < length)
    if relative > absolute:
        for evs in tracks.values():
            if isinstance(evs, list):
                for e in evs:
                    if isinstance(e, dict):
                        e["t"] = _f(e.get("t"), 0.0) + start
    return raw


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


MAX_TOKENS = 8192           # one part of a long scene (<= 60 s) fits well inside this


def call_llm(system, user, api_key, model, provider="anthropic", timeout=120, max_tokens=MAX_TOKENS):
    import httpx
    if provider == "openai":
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={"model": model, "max_tokens": max_tokens,
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
        json={"model": model, "max_tokens": max_tokens, "system": system,
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


def _transient(e):
    """A hiccup worth one retry: timeout, dropped connection, rate limit, overload."""
    try:
        import httpx
    except Exception:
        return False
    if isinstance(e, httpx.TransportError):            # includes timeouts
        return True
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in (408, 409, 425, 429, 500, 502, 503, 504, 529)
    return False


def _retry_after(e, default):
    try:
        v = float(e.response.headers.get("retry-after"))
        return max(1.0, min(20.0, v))
    except Exception:
        return default


def _ask_json(system, user, api_key, model, provider, tries=2, max_tokens=MAX_TOKENS):
    """LLM call -> parsed JSON. Retries a parse/truncation failure once, and a
    transient network/rate-limit/overload error once after a short wait. Other
    errors (bad key, bad request) propagate straight away."""
    import time as _time
    parse_left, net_left = tries, 1
    while True:
        try:
            return extract_json(call_llm(system, user, api_key, model, provider, max_tokens=max_tokens))
        except ValueError:
            parse_left -= 1
            if parse_left <= 0:
                raise
        except Exception as e:
            if net_left <= 0 or not _transient(e):
                raise
            net_left -= 1
            _time.sleep(_retry_after(e, 4.0))


def friendly_error(e):
    """Plain words for why an AI call failed (shown in the app)."""
    try:
        import httpx
        if isinstance(e, httpx.TimeoutException):
            return "The AI took too long to answer. Try again, or try a shorter length."
        if isinstance(e, httpx.TransportError):
            return "Couldn't reach the AI service. Check the Pi's internet connection and try again."
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if code in (401, 403):
                return "The AI service refused the key. Check HD_LLM_API_KEY in backend/.env."
            if code == 429:
                return "The AI service is rate-limiting this key right now. Wait a minute and try again."
            if code in (500, 502, 503, 504, 529):
                return "The AI service is overloaded right now. Try again in a minute."
            return f"The AI service said no ({code}). Check the model name in HD_LLM_MODEL."
    except Exception:
        pass
    if isinstance(e, ValueError):
        return "The AI's answer came back incomplete. Try again."
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg[:160]}" if msg else type(e).__name__


def generate_long(description, duration_s, flow, devices, api_key, model, provider="anthropic",
                  fog_max_s=8.0):
    """A scene longer than one part: outline the arc, write every part in
    parallel (each knows the whole arc and its own opening/closing look), then
    merge and sanitize the lot as one scene. Returns (tracks, name, blurb)."""
    from concurrent.futures import ThreadPoolExecutor
    dur = max(3.0, min(_f(duration_s, 60.0), MAX_SCENE_S))
    flow = flow if flow in FLOWS else "flat"
    secs = sections_for(dur)
    try:
        outline = _ask_json(*build_outline_prompt(description, dur, flow, secs), api_key, model, provider,
                            max_tokens=2048)
        if not isinstance(outline, dict):
            outline = {}
    except ValueError:
        outline = {}                          # write the parts without a shared arc rather than fail
    fog_part = max(1, int(math.ceil(fog_bursts_max(dur) / len(secs))))

    def part(k):
        a, b = secs[k]
        raw = _ask_json(*build_section_prompt(description, dur, flow, devices, secs, k, outline),
                        api_key, model, provider)
        return normalize_part_times(raw if isinstance(raw, dict) else {}, a, b), (a, b)

    with ThreadPoolExecutor(max_workers=min(3, len(secs))) as ex:
        results = list(ex.map(part, range(len(secs))))
    merged = {}
    for raw, (a, b) in results:
        # keep each part to its own window and its own share of fog first, so
        # parts can never overlap and one part can't use up the scene's fog
        tr, _n, _b = sanitize_scene(raw, devices, dur, "flat", fog_max_s,
                                    events_max=event_budget(b - a)[1] * 2,
                                    fog_bursts=fog_part, window=(a, b))
        for dev, evs in tr.items():
            merged.setdefault(dev, []).extend(evs)
    # final pass over the whole scene: flow envelope, fog count, event ceiling
    tracks, _n, _b = sanitize_scene({"tracks": _as_raw(merged)}, devices, dur, flow, fog_max_s)
    name = outline.get("name")
    name = str(name)[:60] if name else None
    blurb = str(outline.get("blurb") or "")[:140]
    return tracks, name, blurb


def _as_raw(tracks):
    """Events from a windowed pass (flat flow, so brightness untouched) handed
    back as plain dicts for the whole-scene pass."""
    return {dev: [dict(e) for e in evs] for dev, evs in tracks.items()}


def extend_tracks(ctx, devices, api_key, model, provider="anthropic", fog_max_s=8.0):
    """Write the events for [ctx.start, ctx.end) that continue an existing
    scene. Sanitized inside that window, with no flow dimming and at most
    ctx.fog_left fog bursts. Returns {device_id: [event dicts]}."""
    a, b = float(ctx["start"]), float(ctx["end"])
    raw = _ask_json(*build_extend_prompt(dict(ctx, devices=devices)), api_key, model, provider)
    raw = normalize_part_times(raw if isinstance(raw, dict) else {}, a, b)
    tracks, _n, _b = sanitize_scene(raw, devices, b, "flat", fog_max_s,
                                    events_max=event_budget(b - a)[1] * 2,
                                    fog_bursts=max(0, int(ctx.get("fog_left", 0))), window=(a, b))
    return tracks


def generate(description, duration_s, flow, devices, api_key, model, provider="anthropic", fog_max_s=8.0):
    """Full pipeline: prompt -> LLM -> parse -> sanitize. Returns (tracks, name, blurb).

    Scenes longer than one part go through generate_long. Retries once on a
    parse failure (model output is stochastic)."""
    if _f(duration_s, 30.0) > SECTION_S:
        return generate_long(description, duration_s, flow, devices, api_key, model, provider, fog_max_s)
    system, user = build_prompt(description, duration_s, flow, devices)
    raw = _ask_json(system, user, api_key, model, provider)
    return sanitize_scene(raw if isinstance(raw, dict) else {}, devices, duration_s, flow, fog_max_s)


def palette_from(description, api_key, model, provider="anthropic"):
    """Small LLM call -> (palette[list of hex], accent hex, mood). Falls back to
    None/None/'driving' on any failure, so the beat-sync composer uses defaults."""
    system = "You choose a colour palette for a Halloween light scene. Reply ONLY with JSON, no prose."
    user = ('Scene idea: "' + (description or "").strip() + '"\n'
            'Return JSON: {"palette":["#RRGGBB", 3-5 vivid saturated colours that fit the mood], '
            '"accent":"#RRGGBB" (a bright hit colour), "mood":"driving"|"eerie"|"frantic"}.')
    try:
        raw = extract_json(call_llm(system, user, api_key, model, provider, max_tokens=1024))
        pal = [c for c in (raw.get("palette") or []) if _hexok(c)]
        pal = [_hexok(c) for c in pal][:5] or None
        acc = _hexok(raw.get("accent"))
        mood = raw.get("mood") if raw.get("mood") in ("driving", "eerie", "frantic") else "driving"
        return pal, acc, mood
    except Exception:
        return None, None, "driving"
