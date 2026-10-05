"""Song show: a light show that follows the band and the lyrics (authoring-time).

LISTEN  ffmpeg splits the song into frequency bands (kick, bass, guitar/mids,
        snare crack, cymbals) and stdlib audioop measures each band's loudness
        every ~11.6 ms. A dynamic-programming beat tracker (Ellis 2007) follows
        the drummer's real tempo, downbeats come from the rock backbeat (kick on
        1 and 3, snare on 2 and 4), and every 16th-note slot gets a strength per
        instrument. No numpy and no machine learning: punchy, sparse rock (the
        target) separates well enough by frequency band.
READ    Timed lyrics from LRCLIB give the song's sections (repeated blocks are
        choruses, wordless stretches are intros, solos and outros), and the AI
        turns what the words mean into a palette per section plus a few accent
        moments. Only times and colours are kept, never the words.
PLAY    compose() gives each light an instrument (drums, bass, rhythm guitar)
        and lays events on the beat grid. The caller runs the usual sanitizer,
        so every fog and strobe safety cap still applies.

This runs only when the operator presses Generate or Extend, never during the
unattended show.
"""
from __future__ import annotations

import audioop
import bisect
import math
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

SR = 22050
HOP = 256
HOP_S = HOP / SR                      # ~11.6 ms per frame

# ffmpeg filter chains (2-pole Butterworth stages; doubled for a steeper slope)
BANDS = {
    "kick": "lowpass=f=90,lowpass=f=90",
    "bass": "highpass=f=55,lowpass=f=250,lowpass=f=250",
    "mid": "highpass=f=300,lowpass=f=3000",
    "crack": "highpass=f=2500,highpass=f=2500,lowpass=f=6000",
    "air": "highpass=f=7000,highpass=f=7000",
}
MIN_BPM, MAX_BPM, PRIOR_BPM = 70.0, 190.0, 120.0


# --------------------------------------------------------------------------
# small numeric helpers (stdlib only)
# --------------------------------------------------------------------------
def _pct(vals, q):
    """q-th percentile (0..100) of a list."""
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * max(0.0, min(100.0, q)) / 100.0
    lo = int(math.floor(k)); hi = min(len(s) - 1, lo + 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _norm01(vals, lo_q=10, hi_q=95):
    lo, hi = _pct(vals, lo_q), _pct(vals, hi_q)
    span = (hi - lo) or 1.0
    return [max(0.0, min(1.0, (v - lo) / span)) for v in vals]


DECODE_WORKERS = 3                    # ffmpeg processes at once (the Pi has 4 cores and plays audio)


def _decode_band(path, chain, max_s, sr=SR):
    ff = shutil.which("ffmpeg")
    if not ff:
        return b""
    nice = shutil.which("nice")
    # low priority, so a scene playing while you author never stutters
    cmd = ([nice, "-n", "10"] if nice else []) + [ff, "-v", "quiet", "-t", f"{float(max_s):.3f}", "-i", str(path)]
    if chain:
        cmd += ["-af", chain]
    cmd += ["-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=180).stdout or b""
    except Exception:
        return b""


def _levels(pcm):
    """Per-hop log loudness."""
    step = HOP * 2
    n = len(pcm) // step
    return [math.log1p(audioop.rms(pcm[i * step:(i + 1) * step], 2) / 64.0) for i in range(n)]


def _flux(lv):
    """Rising edges of the loudness curve (onset strength), normalized to ~[0,1]."""
    out = [0.0] * len(lv)
    for i in range(1, len(lv)):
        d = lv[i] - lv[i - 1]
        if d > 0:
            out[i] = d
    top = _pct([x for x in out if x > 0], 98) or 1.0
    return [min(1.5, x / top) for x in out]


def music_start(path, max_s=60.0):
    """Seconds until the music really starts (skips a video's silent or quiet
    lead-in): the first sound above 3% of the song's typical level that's
    followed by more sound within two seconds."""
    pcm = _decode_band(path, None, max_s, sr=8000)
    frame = 400 * 2                                   # 50 ms at 8 kHz, 16-bit
    rms = [audioop.rms(pcm[i * frame:(i + 1) * frame], 2) for i in range(len(pcm) // frame)]
    if len(rms) < 40:
        return 0.0
    gate = 0.03 * (_pct(rms, 90) or 1)
    for i in range(len(rms) - 40):
        # some sound now, and more than a click in the next two seconds: a sparse
        # guitar intro counts as music (cutting it off would be worse than
        # keeping a little lead-in)
        if rms[i] >= gate and sum(1 for v in rms[i:i + 40] if v >= gate) >= 4:
            return round(max(0.0, i * 0.05 - 0.05), 3)
    return 0.0


# --------------------------------------------------------------------------
# tempo + beats
# --------------------------------------------------------------------------
def _tempo_period(env):
    """Beat period in frames (fractional): onset-envelope autocorrelation over
    MIN..MAX BPM, weighted toward ~120 BPM so a rock song isn't read at half or
    double time, refined with parabolic interpolation."""
    n = len(env)
    lag_min = max(2, int(60.0 / MAX_BPM / HOP_S))
    lag_max = min(n - 2, int(60.0 / MIN_BPM / HOP_S) + 1)
    if lag_max <= lag_min + 2:
        return None
    mean = sum(env) / n
    e = [x - mean for x in env]
    ac = {}
    for lag in range(lag_min - 1, lag_max + 2):
        s = 0.0
        for i in range(lag, n):
            s += e[i] * e[i - lag]
        ac[lag] = s / (n - lag)
    def weighted(lag):
        bpm = 60.0 / (lag * HOP_S)
        return ac[lag] * math.exp(-0.5 * (math.log2(bpm / PRIOR_BPM) / 0.9) ** 2)
    best = max(range(lag_min, lag_max + 1), key=weighted)
    a, b, c = ac[best - 1], ac[best], ac[best + 1]
    den = a - 2 * b + c
    off = 0.5 * (a - c) / den if den else 0.0
    return best + max(-0.5, min(0.5, off))


def _track_beats(env, period, tightness=100.0):
    """Dynamic-programming beat tracking (Ellis 2007): beats land on strong
    onsets while staying close to the tempo, so a real drummer's small
    speed-ups and slow-downs are followed instead of drifting off the grid."""
    n = len(env)
    if n < 4 or not period:
        return []
    sd = (sum(x * x for x in env) / n) ** 0.5 or 1.0
    loc = [x / sd for x in env]
    sm = [0.0] * n                                  # light smoothing
    for i in range(n):
        sm[i] = 0.25 * loc[i - 1 if i else 0] + 0.5 * loc[i] + 0.25 * loc[i + 1 if i + 1 < n else i]
    lo = max(1, int(round(period * 0.75)))
    hi = max(lo + 1, int(round(period * 1.33)))
    pen = {k: -tightness * (math.log(k / period)) ** 2 for k in range(lo, hi + 1)}
    score = [0.0] * n
    back = [-1] * n
    for i in range(n):
        best, arg = 0.0, -1
        for k in range(lo, hi + 1):
            j = i - k
            if j < 0:
                break
            v = score[j] + pen[k]
            if arg < 0 or v > best:
                best, arg = v, j
        score[i] = sm[i] + (best if arg >= 0 and best > 0 else 0.0)
        back[i] = arg if arg >= 0 and best > 0 else -1
    tail = max(1, int(round(period)))
    i = max(range(max(0, n - tail), n), key=lambda j: score[j])
    beats = []
    while i >= 0:
        beats.append(i)
        i = back[i]
    beats.reverse()
    return [round(b * HOP_S, 4) for b in beats]


def _at(arr, t, half_w):
    """Max of a per-hop array within +-half_w seconds of t."""
    a = max(0, int((t - half_w) / HOP_S))
    b = min(len(arr), int((t + half_w) / HOP_S) + 1)
    return max(arr[a:b]) if b > a else 0.0


def _mean_at(arr, t0, t1):
    a = max(0, int(t0 / HOP_S)); b = min(len(arr), max(a + 1, int(t1 / HOP_S)))
    seg = arr[a:b]
    return sum(seg) / len(seg) if seg else 0.0


# --------------------------------------------------------------------------
# the song map
# --------------------------------------------------------------------------
def _empty(tempo=0, beats=()):
    return {"tempo": tempo, "period_s": 0, "beats": list(beats), "downbeats": [], "bars": [], "slots": []}


def analyze(path, duration_s, line_times=None, debug=False, downbeat_hint=None):
    """Song map for the first duration_s seconds of an audio file:
    {tempo, period_s, beats, downbeats, bars:[{t, end, energy, drums}], slots:[16ths]}.
    Each slot: {t, beat, pos (0-15 in the bar), kick, snare, stab (bools),
    stab_s, bass, mid, air (0..1), drums (the bar's drums are playing)}.

    line_times: when lyric lines are known (seconds into this audio), they
    settle which beat is "1": sung lines start on or just before a downbeat.
    downbeat_hint: bar lines found before (Extend re-reads a longer clip of
    the same song); when given, "1" is the beat that lines up with them, so
    the bars don't shift under the part that's already there."""
    dur = float(duration_s)
    with ThreadPoolExecutor(max_workers=DECODE_WORKERS) as ex:
        pcms = dict(zip(BANDS, ex.map(lambda ch: _decode_band(path, ch, dur), BANDS.values())))
    lv = {k: _levels(v) for k, v in pcms.items()}
    n = min((len(v) for v in lv.values()), default=0)
    if n < 100:
        return _empty()
    lv = {k: v[:n] for k, v in lv.items()}
    fx = {k: _flux(v) for k, v in lv.items()}
    env = [fx["kick"][i] + fx["crack"][i] + 0.6 * fx["mid"][i] + 0.4 * fx["air"][i] for i in range(n)]
    beats = [b for b in _track_beats(env, _tempo_period(env)) if b < dur]
    if len(beats) < 8:
        return _empty(beats=beats)
    ivals = sorted(b - a for a, b in zip(beats, beats[1:]))
    med = ivals[len(ivals) // 2]
    tempo = round(60.0 / med, 1) if med else 0.0
    w = med / 8.0                                     # half a 16th either side
    nb = len(beats)

    # Which beat is "1"? The rock backbeat (kick on 1 and 3, snare on 2 and 4)
    # picks {1,3} vs {2,4}; then sung lines (or chord changes and crashes)
    # pick 1 over 3.
    kick_b = [_at(fx["kick"], b, w) for b in beats]
    crack_b = [_at(fx["crack"], b, w) for b in beats]
    backbeat = lambda k: sum((kick_b[j] - crack_b[j]) * (1 if (j - k) % 2 == 0 else -1) for j in range(nb))
    pair = 0 if backbeat(0) >= backbeat(1) else 1
    change_b = [_at(fx["mid"], b, w) + _at(fx["air"], b, w) + _at(fx["bass"], b, w) for b in beats]
    novelty = lambda k: (sum(change_b[j] for j in range(k, nb, 4)) - sum(change_b[j] for j in range(k + 2, nb, 4)))
    phase = pair if novelty(pair) >= novelty(pair + 2) else pair + 2
    def misfit(k, times):
        dbs = [beats[j] for j in range(k, nb, 4)]
        tot = 0.0
        for t in times:
            i = bisect.bisect_left(dbs, t)
            tot += min([abs(dbs[x] - t) for x in (i - 1, i) if 0 <= x < len(dbs)] + [2 * med])
        return tot
    lines = sorted(t for t in (line_times or []) if 0 <= t < dur)
    hint = sorted(float(t) for t in (downbeat_hint or []) if isinstance(t, (int, float)) and 0 < t < dur)
    if len(hint) >= 2:
        phase = min(range(4), key=lambda k: misfit(k, hint))
    elif len(lines) >= 4:
        a, b = misfit(pair, lines), misfit(pair + 2, lines)
        if abs(a - b) > 0.15 * max(a, b):            # a clear answer from the words
            phase = pair if a < b else pair + 2
    downbeats = [beats[j] for j in range(phase, nb, 4)]

    # 16th slots
    raw = []
    for j, b in enumerate(beats):
        nxt = beats[j + 1] if j + 1 < nb else b + med
        q = (nxt - b) / 4.0
        for sx in range(4):
            t = b + sx * q
            if t >= dur:
                break
            i0 = int(t / HOP_S)
            pk = max(lv["kick"][i0:i0 + 3] or [0.0])
            later = lv["kick"][min(n - 1, i0 + int(0.09 / HOP_S))]
            raw.append({"t": round(t, 4), "beat": j, "pos": ((j - phase) % 4) * 4 + sx,
                        "kick": _at(fx["kick"], t, q / 2), "kdecay": pk - later,
                        "crack": _at(fx["crack"], t, q / 2), "stab": _at(fx["mid"], t, q / 2),
                        "bass": _mean_at(lv["bass"], t, t + q), "mid": _mean_at(lv["mid"], t, t + q),
                        "air": _mean_at(lv["air"], t, t + q)})
    if not raw:
        return _empty(tempo, beats)
    t_fill = max(0.7, _pct([r["crack"] for r in raw], 98))
    t_kick_x = max(0.5, _pct([r["kick"] for r in raw if r["pos"] % 2 == 0], 85))
    t_stab = max(0.25, _pct([r["stab"] for r in raw], 70))
    for key in ("bass", "mid", "air"):
        for r, v in zip(raw, _norm01([r[key] for r in raw])):
            r[key] = round(v, 3)

    # bars: is the kit playing, and how hard is the band going? Cymbals and
    # hats (the air band) are the surest sign of a drummer; a clear backbeat
    # counts too. (In loud choruses the snare drowns in guitar, so its own
    # level can't be trusted to say whether the drums are there.)
    bars, st = [], [r["t"] for r in raw]
    mean = lambda seg, key, poss: (lambda v: sum(v) / len(v) if v else 0.0)([r[key] for r in seg if r["pos"] in poss])
    for i, db in enumerate(downbeats):
        end = downbeats[i + 1] if i + 1 < len(downbeats) else min(dur, db + 4 * med)
        seg = raw[bisect.bisect_left(st, db):bisect.bisect_left(st, end)]
        if not seg:
            continue
        air = sum(r["air"] for r in seg) / len(seg)
        contrast = (mean(seg, "kick", (0, 8)) - mean(seg, "kick", (4, 12))
                    + mean(seg, "crack", (4, 12)) - mean(seg, "crack", (0, 8)))
        e = sum(0.35 * r["bass"] + 0.4 * r["mid"] + 0.25 * r["air"] for r in seg) / len(seg)
        bars.append({"t": round(db, 4), "end": round(end, 4), "energy": e,
                     "drums": air >= 0.25 or contrast >= 0.4})
    for b, e in zip(bars, _norm01([b["energy"] for b in bars], 5, 95)):
        b["energy"] = round(e, 3)
    slots, k = [], 0
    for r in raw:
        while k + 1 < len(bars) and r["t"] >= bars[k + 1]["t"]:
            k += 1
        drums = bool(bars and bars[k]["t"] <= r["t"] < bars[k]["end"] and bars[k]["drums"])
        # While the kit plays: the rock backbeat (kick on 1 and 3, snare on 2
        # and 4) always, plus what the bands show on top: extra kicks that hit
        # hard and die fast (bass notes share the kick's band but ring on),
        # and snare fills.
        kick = drums and r["pos"] % 2 == 0 and (r["pos"] in (0, 8) or (r["kick"] >= t_kick_x and r["kdecay"] >= 0.35))
        snare = drums and (r["pos"] in (4, 12) or r["crack"] >= t_fill)
        # guitar stab: the mid band leads the hit. A drum hit also lights up the
        # mids, so on a kick or snare the mids must clearly beat the drum band.
        lead = r["stab"] >= 1.15 * r["crack"] and (not kick or r["stab"] >= 1.15 * r["kick"])
        stab = r["stab"] >= t_stab and (lead if (kick or snare) else (r["stab"] >= 0.9 * r["crack"]))
        slot = {"t": r["t"], "beat": r["beat"], "pos": r["pos"], "kick": kick, "snare": snare,
                "stab": stab, "stab_s": round(min(1.0, r["stab"]), 3),
                "bass": r["bass"], "mid": r["mid"], "air": r["air"], "drums": drums}
        if debug:
            slot.update({"_kick": round(r["kick"], 3), "_crack": round(r["crack"], 3),
                         "_kdecay": round(r["kdecay"], 3), "_stab": round(r["stab"], 3)})
        slots.append(slot)
    return {"tempo": tempo, "period_s": round(med, 4), "beats": beats, "downbeats": downbeats,
            "bars": bars, "slots": slots}


# --------------------------------------------------------------------------
# lyrics (LRCLIB): timed lines, kept in memory only
# --------------------------------------------------------------------------
LRCLIB = "https://lrclib.net/api/"
USER_AGENT = "HauntDirector/0.2 (https://github.com/phonoodlesramen/haunt-director)"
_TS = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_NOISE = re.compile(r"[\(\[][^\)\]]*(official|video|audio|lyric|lyrics|remaster|remastered|hd|hq|live|"
                    r"version|visualizer|4k|mv)[^\)\]]*[\)\]]", re.I)


def parse_lrc(text):
    """'[mm:ss.xx] words' lines -> [(seconds, words)], sorted. Blank timed lines
    (the end of a line in many files) are dropped; metadata tags are ignored."""
    out = []
    if not isinstance(text, str):
        return out                                 # a malformed entry, not a crash
    for raw in text.splitlines():
        stamps = _TS.findall(raw)
        words = _TS.sub("", raw).strip()
        if not stamps or not words:
            continue
        for mm, ss in stamps:
            out.append((round(int(mm) * 60 + float(ss), 2), words))
    out.sort()
    return out


def split_title(title):
    """'Some Band - Night Ride (Official Video)' -> ('Some Band', 'Night Ride')."""
    t = _NOISE.sub("", title or "")
    t = re.sub(r"\s+", " ", t).strip(" -|")
    for sep in (" - ", " – ", " — ", " | ", ": "):
        if sep in t:
            a, b = t.split(sep, 1)
            return a.strip(), b.strip(" -|")
    return "", t


def _lrclib_search(params, timeout=15):
    import httpx
    r = httpx.get(LRCLIB + "search", params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    return d if isinstance(d, list) else []


def pick_lyrics(candidates, song_len, tol=6.0):
    """Choose the timed lyrics that best fit this recording. Entries are
    community-made and their timings disagree by a second or two, so among the
    ones whose length matches the song, take the one closest to the others (the
    medoid of their line timings) rather than just the first."""
    ok = []
    for c in candidates or []:
        if not isinstance(c, dict) or c.get("instrumental"):
            continue
        lines = parse_lrc(c.get("syncedLyrics") or "")
        dur = c.get("duration")
        if len(lines) < 4 or not isinstance(dur, (int, float)):
            continue
        if song_len and abs(float(dur) - float(song_len)) > tol:
            continue
        ok.append((c, lines))
    if not ok:
        return None
    def dist(a, b):
        k = min(len(a), len(b), 12)
        return sorted(abs(a[i][0] - b[i][0]) for i in range(k))[k // 2] + 0.5 * abs(len(a) - len(b))
    best = min(ok, key=lambda x: sum(dist(x[1], y[1]) for y in ok))
    c, lines = best
    return {"id": c.get("id"), "track": c.get("trackName") or c.get("name"), "artist": c.get("artistName"),
            "duration": c.get("duration"), "lines": lines}


def find_lyrics(artist, track, song_len, search=_lrclib_search):
    """Timed lyrics for a song from LRCLIB, or None (not found, instrumental,
    offline). Never raises."""
    tries = []
    if track:
        tries.append({"track_name": track, "artist_name": artist} if artist else {"track_name": track})
    q = " ".join(x for x in (artist, track) if x)
    if q:
        tries.append({"q": q})
    for params in tries:
        try:
            got = pick_lyrics(search(params), song_len)
        except Exception:
            got = None
        if got:
            return got
    return None


def _lrclib_get(lid, timeout=15):
    import httpx
    r = httpx.get(LRCLIB + f"get/{int(lid)}", headers={"User-Agent": USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    return d if isinstance(d, dict) else {}


def lyrics_by_id(lid, get=_lrclib_get):
    """The same timed lines again (Extend), by the LRCLIB id saved with the
    scene; [] when there's no id or LRCLIB can't be reached. Never raises."""
    if isinstance(lid, bool) or not isinstance(lid, int):
        return []
    try:
        return parse_lrc((get(lid) or {}).get("syncedLyrics"))
    except Exception:
        return []


# --------------------------------------------------------------------------
# sections: from the words when we have them, else from the music
# --------------------------------------------------------------------------
def _norm_line(s):
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def _snap(t, grid):
    if not grid:
        return t
    i = bisect.bisect_left(grid, t)
    c = [grid[x] for x in (i - 1, i) if 0 <= x < len(grid)]
    return min(c, key=lambda g: abs(g - t))


def _bar_energy(songmap, a, b):
    es = [x["energy"] for x in songmap.get("bars", []) if a <= x["t"] < b]
    return round(sum(es) / len(es), 3) if es else 0.5


def sections_from_lyrics(lines, songmap, dur):
    """Repeated lines are the chorus; runs of the same kind become sections;
    long wordless gaps become instrumental sections (a solo if the band is
    going hard). Boundaries land on bar lines."""
    db = list(songmap.get("downbeats") or [])
    bar = (songmap.get("period_s") or 0.5) * 4
    lines = [(t, w) for t, w in lines if 0 <= t < dur]
    if len(lines) < 4 or not db:
        return sections_from_music(songmap, dur)
    count = {}
    for _, w in lines:
        count[_norm_line(w)] = count.get(_norm_line(w), 0) + 1
    rep = [count[_norm_line(w)] >= 2 for _, w in lines]
    runs = []                                   # (first_idx, last_idx, repeated)
    for i, (t, _) in enumerate(lines):
        gap = t - lines[i - 1][0] if i else 0
        if runs and rep[i] == runs[-1][2] and gap < max(7.0, 3 * bar):
            runs[-1][1] = i
        else:
            runs.append([i, i, rep[i]])
    secs = []
    first = _snap(lines[runs[0][0]][0], db)
    if first >= bar * 0.9:
        secs.append({"start": 0.0, "kind": "intro", "lines": []})
    for k, (a, b, r) in enumerate(runs):
        start = _snap(lines[a][0], db) if (secs or first >= bar * 0.9) else 0.0
        if secs and start <= secs[-1]["start"]:
            secs[-1]["lines"] += list(range(a, b + 1))
            continue
        secs.append({"start": start, "kind": "chorus" if r else "verse", "lines": list(range(a, b + 1))})
        # a long stretch with no words before the next run (or the end)
        last_t = lines[b][0]
        nxt = lines[runs[k + 1][0]][0] if k + 1 < len(runs) else dur
        quiet_from = _snap(last_t + max(3.0, bar), db)
        if nxt - quiet_from >= 4 * bar - 0.25 and quiet_from < dur - bar:
            secs.append({"start": quiet_from, "kind": "outro" if k + 1 == len(runs) else "instrumental",
                         "lines": []})
    for i, s in enumerate(secs):
        s["end"] = secs[i + 1]["start"] if i + 1 < len(secs) else dur
    secs = _tidy(secs, bar)
    for i, s in enumerate(secs):
        s["energy"] = _bar_energy(songmap, s["start"], s["end"])
        if s["kind"] == "instrumental" and i == len(secs) - 1 and i > 0:
            s["kind"] = "outro"
        elif s["kind"] == "instrumental" and s["energy"] >= 0.55 and s["start"] > dur * 0.35:
            s["kind"] = "solo"
    return [dict(s, start=round(s["start"], 3), end=round(s["end"], 3)) for s in secs if s["end"] - s["start"] > 0.5]


def _tidy(secs, bar):
    """Fold away fragments: a one-line 'verse' between choruses (an ad-lib),
    anything under two bars, and neighbours of the same kind."""
    out = []
    for s in secs:
        short = s["end"] - s["start"] < 2 * bar - 0.05
        adlib = s["kind"] == "verse" and len(s["lines"]) <= 1 and out and out[-1]["kind"] == "chorus"
        if out and (short or adlib or s["kind"] == out[-1]["kind"]):
            out[-1]["end"] = s["end"]
            out[-1]["lines"] = out[-1]["lines"] + s["lines"]
        else:
            out.append(dict(s, lines=list(s["lines"])))
    return out


def sections_from_music(songmap, dur):
    """No words to go on: cut where the band's energy changes (on 4-bar
    phrases), and call the loud parts choruses."""
    bars = songmap.get("bars") or []
    if len(bars) < 8:
        return [{"start": 0.0, "end": round(dur, 3), "kind": "verse", "lines": [],
                 "energy": _bar_energy(songmap, 0, dur)}]
    e = [b["energy"] for b in bars]
    nov = [0.0] * len(bars)
    for i in range(4, len(bars) - 4):
        nov[i] = abs(sum(e[i:i + 4]) / 4 - sum(e[i - 4:i]) / 4)
    mean = sum(nov) / len(nov)
    cuts, last = [0], 0
    for i in range(4, len(bars) - 4):
        if i % 4 == 0 and nov[i] > mean * 1.2 and i - last >= 8:
            cuts.append(i); last = i
    secs = []
    for k, c in enumerate(cuts):
        start = 0.0 if k == 0 else bars[c]["t"]
        end = bars[cuts[k + 1]]["t"] if k + 1 < len(cuts) else dur
        secs.append({"start": round(start, 3), "end": round(end, 3), "lines": [],
                     "energy": _bar_energy(songmap, start, end)})
    hi = _pct([s["energy"] for s in secs], 60)
    for i, s in enumerate(secs):
        s["kind"] = ("intro" if i == 0 and len(secs) > 2 else "outro" if i == len(secs) - 1 and len(secs) > 2
                     else "chorus" if s["energy"] >= hi else "verse")
    return secs


# --------------------------------------------------------------------------
# colours from meaning (the AI)
# --------------------------------------------------------------------------
DEFAULT_LOOKS = {
    "intro": (["#5a0a0a", "#2a0a3a", "#ff5a1e"], "#ff2a00"),
    "verse": (["#ff5a1e", "#3a0a6a", "#ffb000"], "#ffffff"),
    "chorus": (["#ff1a00", "#c8003c", "#ff7a00"], "#ffffff"),
    "solo": (["#8b2fff", "#ff1a00", "#ffffff"], "#ffffff"),
    "instrumental": (["#2ea6ff", "#3a0a6a", "#c8003c"], "#ffffff"),
    "outro": (["#ff1a00", "#5a0a0a", "#ffffff"], "#ffffff"),
}


def _hex(c):
    if isinstance(c, str) and re.fullmatch(r"#?[0-9a-fA-F]{6}", c.strip()):
        return "#" + c.strip().lstrip("#").lower()
    return None


def _lit(c):
    """A colour for a coloured light: the same hue at full strength. On a PAR a
    dark hex like #1a1a2a just looks switched off; how bright a light is comes
    from its brightness (the section's energy), not from the colour. Near-black
    isn't a colour for a light at all (None)."""
    h = _hex(c)
    if not h:
        return None
    rgb = [int(h[i:i + 2], 16) for i in (1, 3, 5)]
    top = max(rgb)
    if top < 12:
        return None
    return "#" + "".join("%02x" % min(255, int(round(v * 255.0 / top))) for v in rgb)


DEFAULT_LOOKS = {k: ([_lit(c) for c in pal], _lit(acc)) for k, (pal, acc) in DEFAULT_LOOKS.items()}


def build_color_prompt(track, artist, sections, lines, direction=""):
    secs = []
    for i, s in enumerate(sections):
        words = "; ".join(f"[{j}] ({lines[j][0]:.1f}s) {lines[j][1]}" for j in s.get("lines", []) if j < len(lines))
        secs.append(f"{i}. {s['start']:.1f}-{s['end']:.1f} s  {s['kind']}  (band energy {s['energy']:.2f})"
                    + (f": {words}" if words else "  (no words)"))
    song = f'"{track}"' + (f" by {artist}" if artist else "")
    user = f"""\
You're lighting a home Halloween yard to a song: {song}.
Three lights follow the band: the first the drums, the second the bass, the third the rhythm
guitar; a fog machine marks big moments. The beat and the instruments already decide WHEN the
lights hit. Your job is colour and mood: what the words and the music mean.

SECTIONS (seconds, kind, how hard the band is playing 0-1, and the lines sung in each):
{chr(10).join(secs)}

For each section give a palette of 3 colours (#RRGGBB) in order [drums light, bass light,
guitar light], one accent colour for the biggest hits, and an energy 0-1 (how hard the lights
should hit). These are coloured stage lights, so give vivid colours with the brightest channel
at or near ff: a dark or quiet mood is a low energy and a deep hue (deep blue #0020ff, blood red
#ff0000, violet #6000ff), never a dark hex like #1a1a2a, which just looks switched off. Choose
colours from what the words mean and how the music feels, and keep it Halloween: fire and hell
in reds and oranges, night roads in deep blues with amber, ghosts in cold whites and greens. Repeated choruses share one look so people recognise them.
Also pick up to 8 key lines to accent (the hook, a shouted word, a dramatic turn): the line
number, a colour, and an effect: "flash" (every light hits the colour as the line starts) or
"blackout" (a beat of darkness, then the flash).
{('EXTRA DIRECTION FROM THE PERSON RUNNING THE SHOW: "' + direction.strip() + '"') if (direction or "").strip() else ""}
Do not quote the lyrics anywhere in your reply.
Return ONLY this JSON:
{{"theme": "<one short line about the look, no lyrics>",
  "sections": [{{"i": 0, "palette": ["#rrggbb", "#rrggbb", "#rrggbb"], "accent": "#rrggbb", "energy": 0.7}}],
  "moments": [{{"line": 0, "color": "#rrggbb", "effect": "flash"}}]}}"""
    system = ("You are a lighting designer for a home Halloween haunt. You answer with strict JSON "
              "only, and you never quote song lyrics.")
    return system, user


def apply_colors(sections, lines, raw):
    """Merge the AI's answer into the sections (defaults where it's missing or
    malformed). Returns (sections, moments [(t, colour, effect)], theme)."""
    raw = raw if isinstance(raw, dict) else {}
    as_list = lambda v: v if isinstance(v, list) else []
    is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)
    by_i = {}
    for x in as_list(raw.get("sections")):
        if isinstance(x, dict) and is_int(x.get("i")):
            by_i[x["i"]] = x
    out = []
    for i, s in enumerate(sections):
        pal, acc = DEFAULT_LOOKS.get(s["kind"], DEFAULT_LOOKS["verse"])
        x = by_i.get(i, {})
        p = [h for h in (_lit(c) for c in as_list(x.get("palette"))) if h][:3]
        while p and len(p) < 3:
            p.append(p[-1])
        try:
            en = float(x.get("energy"))
            en = max(0.0, min(1.0, en)) if math.isfinite(en) else s["energy"]
        except (TypeError, ValueError):
            en = s["energy"]
        out.append(dict(s, palette=p or list(pal), accent=_lit(x.get("accent")) or acc, energy=round(en, 3)))
    moments = []
    for m in as_list(raw.get("moments"))[:8]:
        if not isinstance(m, dict):
            continue
        j = m.get("line")
        col = _lit(m.get("color"))
        if is_int(j) and 0 <= j < len(lines) and col:
            moments.append((lines[j][0], col, "blackout" if m.get("effect") == "blackout" else "flash"))
    theme = raw.get("theme")
    theme = re.sub(r"\s+", " ", theme).strip()[:120] if isinstance(theme, str) else ""
    if _quotes(theme, lines):
        theme = ""                                   # it borrowed the words: keep none of it
    return out, moments, theme


def _quotes(text, lines, n=3):
    """True if `text` repeats any run of n words from the lyric lines (so a
    one-line design note can never carry the lyrics into a saved scene)."""
    words = _norm_line(text).split()
    if len(words) < n:
        return False
    grams = {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}
    for _, w in lines:
        lw = _norm_line(w).split()
        if any(tuple(lw[i:i + n]) in grams for i in range(len(lw) - n + 1)):
            return True
    return False


# --------------------------------------------------------------------------
# choreography: each light plays an instrument
# --------------------------------------------------------------------------
def roles_for(lights):
    """drums, bass, rhythm guitar on the first three lights (in the rig's
    order); any more lights double the guitar and bass. Two lights: drums and
    guitar. One light: it plays drums and guitar."""
    lights = list(lights)
    if not lights:
        return {}
    if len(lights) == 1:
        return {lights[0]: "drums+guitar"}
    if len(lights) == 2:
        return {lights[0]: "drums", lights[1]: "guitar"}
    roles = {lights[0]: "drums", lights[1]: "bass", lights[2]: "guitar"}
    for i, d in enumerate(lights[3:]):
        roles[d] = "guitar" if i % 2 == 0 else "bass"
    return roles


ROLES = ("drums", "bass", "guitar", "drums+guitar")


def _ev(typ, t, params=None, dur=0.0, keep=False):
    e = {"t": round(max(0.0, t), 3), "type": typ, "duration_s": round(max(0.0, dur), 3),
         "params": dict(params or {})}
    if keep:
        e["_keep"] = True                         # an accent or the ending: never thinned out below
    return e


def ending_plan(songmap, sections, duration):
    """Where the song ends: (tf, strobe_from). tf is the final downbeat (the
    last hit); strobe_from is where two bars of strobe into it start (None
    when the last section isn't loud or long enough). (None, None): no ending."""
    beat = songmap.get("period_s") or 0.5
    bars = songmap.get("bars") or []
    if not sections or not bars:
        return None, None
    finals = [x["t"] for x in bars if x["t"] < float(duration) - beat]
    if not finals:
        return None, None
    tf = finals[-1]
    last = sections[-1]
    sf = tf - 8 * beat
    return tf, (sf if last["energy"] >= 0.7 and sf > last["start"] else None)


def ending_from(songmap, sections, duration):
    """When the ending's choreography starts (None if there's no ending)."""
    tf, sf = ending_plan(songmap, sections, duration)
    return sf if sf is not None else tf


def compose(songmap, sections, moments, lights, fogs, duration, roles=None):
    """A light show on the song map. Returns ({device: [events]}, roles).
    roles: {light: role} to keep (Extend keeps the scene's own); by default
    they follow the rig's order. Raw events: the caller sanitizes them
    (fog/strobe caps, rates, counts)."""
    dur = float(duration)
    lights = list(lights)
    roles = {d: r for d, r in (roles or {}).items() if d in lights and r in ROLES} or roles_for(lights)
    beat = songmap.get("period_s") or 0.5
    slots = songmap.get("slots") or []
    bars = songmap.get("bars") or []
    beats = songmap.get("beats") or []
    tr = {d: [] for d in roles}
    color_of = lambda role, pal: pal[{"drums": 0, "drums+guitar": 0, "bass": 1, "guitar": 2}[role] % len(pal)]
    plays = lambda role, inst: inst in role.split("+")
    st = [s["t"] for s in slots]
    # Windows where only an accent or the ending plays on the lights. A strobe
    # or a beat of darkness lasts only until the light's next event, so the
    # instruments step aside there instead of chopping it short.
    quiet = []
    tf, sf = ending_plan(songmap, sections, dur)
    end_from = sf if sf is not None else tf
    if end_from is not None:
        quiet.append((end_from - beat / 4, dur + 1.0))

    for k, sec in enumerate(sections):
        a, b = sec["start"], min(sec["end"], dur)
        pal, acc, en, kind = sec["palette"], sec["accent"], sec["energy"], sec["kind"]
        base = 0.10 + 0.12 * en
        for d, role in roles.items():
            col = color_of(role, pal)
            if k > 0 and en >= 0.45:                    # a crash into the new section
                tr[d].append(_ev("set_color", a, {"color": acc, "brightness": 1.0}))
                tr[d].append(_ev("fade_color", a + 0.02, {"color": col, "brightness": base, "curve": "ease_out"},
                                 1.5 * beat))
            else:
                tr[d].append(_ev("fade_color", a, {"color": col, "brightness": base, "curve": "ease_in_out"},
                                 min(1.5, 2 * beat)))
        seg = slots[bisect.bisect_left(st, a + 0.03):bisect.bisect_left(st, b)]

        if kind == "solo":                              # a chase round the yard
            order = list(roles)
            n = 0
            for s in seg if order else ():
                if not (s["stab"] or s["pos"] % 2 == 0):
                    continue
                d = order[n % len(order)]; n += 1
                c = acc if s["pos"] == 0 else pal[n % len(pal)]
                tr[d].append(_ev("set_color", s["t"], {"color": c, "brightness": 0.95}))
                tr[d].append(_ev("fade_color", s["t"] + 0.02, {"color": color_of(roles[d], pal), "brightness": base,
                                                               "curve": "ease_out"}, beat * 0.45))
            continue

        last_stab = -1.0
        for s in seg:
            t = s["t"]
            drawn = False
            for d, role in roles.items():
                if plays(role, "drums") and (s["snare"] or s["kick"]):
                    if s["snare"]:
                        tr[d].append(_ev("set_color", t, {"color": "#ffffff" if en >= 0.6 else acc, "brightness": 1.0}))
                        fade = 0.5 * beat
                    else:
                        tr[d].append(_ev("set_color", t, {"color": pal[0], "brightness": 0.55 + 0.45 * en}))
                        fade = 0.4 * beat
                    tr[d].append(_ev("fade_color", t + 0.02, {"color": pal[0], "brightness": base, "curve": "ease_out"},
                                     fade))
                elif plays(role, "guitar") and s["stab"] and t - last_stab >= beat * 0.45 \
                        and not (role == "drums+guitar" and (s["snare"] or s["kick"])):
                    lvl = 0.5 + 0.5 * max(s["stab_s"], s["mid"])
                    gcol = color_of(role, pal) if role != "drums+guitar" else pal[2 % len(pal)]
                    tr[d].append(_ev("set_color", t, {"color": gcol, "brightness": min(1.0, lvl * (0.7 + 0.3 * en))}))
                    tr[d].append(_ev("fade_color", t + 0.02, {"color": gcol, "brightness": base * 0.6,
                                                              "curve": "ease_out"}, 0.6 * beat))
                    drawn = True
            if drawn:                                   # spacing counts from the last hit drawn
                last_stab = t

        # bass: one throb per bar, peaking on every 8th note
        bass_lights = [d for d, r in roles.items() if plays(r, "bass")]
        if bass_lights:
            e8 = beat / 2
            for bar in bars:
                if not (a <= bar["t"] < b):
                    continue
                sl = slots[bisect.bisect_left(st, bar["t"]):bisect.bisect_left(st, bar["end"])]
                lvl = sum(x["bass"] for x in sl) / len(sl) if sl else 0.0
                for d in bass_lights:
                    if lvl >= 0.2:
                        t0 = bar["t"] - e8 / 4
                        tr[d].append(_ev("pulse", t0, {"color": pal[1 % len(pal)], "rate_hz": round(1.0 / e8, 3),
                                                        "brightness": round(min(1.0, (0.35 + 0.65 * lvl) * (0.6 + 0.4 * en)), 3)},
                                         min(bar["end"], b) - t0))
                    else:
                        tr[d].append(_ev("fade_color", bar["t"], {"color": pal[1 % len(pal)], "brightness": base,
                                                                  "curve": "ease_out"}, beat))

        if kind == "chorus" and en >= 0.5:
            for f in fogs:
                tr.setdefault(f, []).append(_ev("fog", a, {"intensity": 0.7}, 3.5))

    # the AI's accent lines (none in the run-up to the ending)
    for t, col, eff in moments:
        if not (0 <= t < dur):
            continue
        tb = _snap(t, beats) if beats else t
        if end_from is not None and tb >= end_from - beat:
            continue
        sec = next((s for s in sections if s["start"] <= tb < s["end"]), sections[-1] if sections else None)
        pal = sec["palette"] if sec else ["#ff1a00"] * 3
        base = 0.10 + 0.12 * (sec["energy"] if sec else 0.5)
        dark = eff == "blackout" and tb - beat > 0
        quiet.append(((tb - beat) if dark else tb, tb + 0.5 * beat))
        for d, role in roles.items():
            if dark:
                tr[d].append(_ev("blackout", tb - beat, keep=True))
            tr[d].append(_ev("set_color", tb, {"color": col, "brightness": 1.0}, keep=True))
            tr[d].append(_ev("fade_color", tb + 0.02, {"color": color_of(role, pal), "brightness": base,
                                                       "curve": "ease_out"}, beat, keep=True))

    # the ending: two bars of strobe (if the song goes out loud), a last hit on
    # the final downbeat, then dark
    if tf is not None:
        last = sections[-1]
        for d in roles:
            if sf is not None:
                tr[d].append(_ev("strobe", sf, {"color": last["accent"], "brightness": 1.0,
                                                "rate_hz": round(2.0 / beat, 2)}, min(6.0, 8 * beat - 0.05), keep=True))
            tr[d].append(_ev("set_color", tf, {"color": last["accent"], "brightness": 1.0}, keep=True))
            tr[d].append(_ev("fade_brightness", tf + 0.02, {"brightness": 0.0, "curve": "ease_out"},
                             min(dur - tf - 0.05, 2 * beat), keep=True))

    for d in roles:
        if quiet:
            tr[d] = [e for e in tr[d] if e.get("_keep") or not any(qa - 0.01 <= e["t"] < qb for qa, qb in quiet)]
    for d in tr:
        for e in tr[d]:
            e.pop("_keep", None)
        tr[d].sort(key=lambda e: e["t"])
    return {d: v for d, v in tr.items() if v}, roles


def _clean_section(s, dur):
    """A stored section, checked (it lives in the scene file)."""
    kind = s.get("kind") if isinstance(s.get("kind"), str) and s.get("kind") in DEFAULT_LOOKS else "verse"
    pal, acc = DEFAULT_LOOKS[kind]
    p = [h for h in (_hex(c) for c in (s.get("palette") if isinstance(s.get("palette"), list) else [])) if h][:3]
    try:
        en = max(0.0, min(1.0, float(s.get("energy"))))
    except (TypeError, ValueError):
        en = 0.5
    try:
        a, b = float(s["start"]), float(s["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(a) and math.isfinite(b)) or b <= a or a >= dur:
        return None
    return {"start": round(max(0.0, a), 3), "end": round(min(b, dur), 3), "kind": kind,
            "palette": p or list(pal), "accent": _hex(s.get("accent")) or acc, "energy": round(en, 3)}


def extend_sections(stored, songmap, lines, old, new_dur):
    """Sections for a song show made longer: the ones it already has, with
    their colours, then new ones for the rest of the song from the words (or
    the music). A new section takes the look of the scene's existing section
    of the same kind, so a returning chorus looks like the chorus."""
    dur, old = float(new_dur), float(old)
    bar = (songmap.get("period_s") or 0.5) * 4
    keep = [c for c in (_clean_section(s, old) for s in stored or [] if isinstance(s, dict)) if c]
    keep.sort(key=lambda s: s["start"])
    if not keep:
        pal, acc = DEFAULT_LOOKS["chorus"]
        keep = [{"start": 0.0, "end": old, "kind": "chorus", "palette": list(pal), "accent": acc, "energy": 0.8}]
    looks = {}
    for s in keep:
        looks.setdefault(s["kind"], s)
    fresh = sections_from_lyrics(lines, songmap, dur) if len(lines or []) >= 4 else sections_from_music(songmap, dur)
    last = keep[-1]
    nxt = [s for s in fresh
           if s["start"] >= max(old - 0.5 * bar, last["start"] + 2 * bar) and s["start"] < dur - 0.5]
    last["end"] = round(nxt[0]["start"] if nxt else dur, 3)
    for i, s in enumerate(nxt):
        like = looks.get(s["kind"])
        pal, acc = (like["palette"], like["accent"]) if like else DEFAULT_LOOKS.get(s["kind"], DEFAULT_LOOKS["verse"])
        keep.append({"start": round(s["start"], 3),
                     "end": round(nxt[i + 1]["start"] if i + 1 < len(nxt) else dur, 3),
                     "kind": s["kind"], "palette": list(pal), "accent": acc,
                     "energy": like["energy"] if like else s["energy"]})
    return keep


# --------------------------------------------------------------------------
# the whole thing
# --------------------------------------------------------------------------
def build(clip_path, duration, lines, track, artist, lights, fogs, ask_json=None, direction=""):
    """Song map + sections + colours + choreography for one clip.
    lines: [(seconds into this clip, words)] (may be empty).
    ask_json(system, user) -> dict: the AI call (None = default colours).
    Returns {tracks, roles, sections, tempo, theme, beats, downbeats,
    ending_from, lyrics_used, colors_from_ai}."""
    dur = float(duration)
    lines = [(t, w) for t, w in (lines or []) if 0 <= t < dur]
    sm = analyze(clip_path, dur, [t for t, _ in lines])
    if len(sm.get("beats") or []) < 8 or not sm.get("bars"):
        raise ValueError("no steady beat found")
    secs = sections_from_lyrics(lines, sm, dur) if len(lines) >= 4 else sections_from_music(sm, dur)
    raw = None
    if ask_json is not None:
        try:
            raw = ask_json(*build_color_prompt(track or "this song", artist, secs, lines, direction))
        except Exception:
            raw = None                                   # default looks rather than no show
    try:
        secs, moments, theme = apply_colors(secs, lines, raw)
    except Exception:                                    # an answer shaped in some way we didn't foresee
        raw = None
        secs, moments, theme = apply_colors(secs, lines, None)
    tracks, roles = compose(sm, secs, moments, lights, fogs, dur)
    return {"tracks": tracks, "roles": roles, "tempo": sm["tempo"], "theme": theme, "period_s": sm["period_s"],
            "sections": [{k: s[k] for k in ("start", "end", "kind", "palette", "accent", "energy")} for s in secs],
            "beats": len(sm["beats"]), "downbeats": [round(x, 3) for x in sm["downbeats"]],
            "ending_from": ending_from(sm, secs, dur),
            "lyrics_used": len(lines) >= 4, "colors_from_ai": raw is not None}
