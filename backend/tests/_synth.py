"""Synthetic rock track for the song-show tests (no real music needed): 116 BPM drifting to 118, 4/4.
Bars 0-3: guitar stabs only. Bars 4+: kick 1&3, snare 2&4, hats on 8ths, bass 8ths,
guitar stabs on the 'and' of 1 and 3."""
import math, random, struct, sys, wave
SR = 22050
def make(path, bars=24, bpm0=116.0, bpm1=118.0, seed=3, lead=0.5):
    rnd = random.Random(seed)
    # beat times with a linear tempo drift
    beats = []; t = lead
    total_beats = bars * 4
    for i in range(total_beats):
        beats.append(t)
        bpm = bpm0 + (bpm1 - bpm0) * i / total_beats
        t += 60.0 / bpm
    end = t + 1.0
    n = int(end * SR)
    buf = [0.0] * n
    def add(t0, dur, fn, gain):
        a = int(t0 * SR); b = min(n, a + int(dur * SR))
        for k in range(a, b):
            tt = (k - a) / SR
            buf[k] += gain * fn(tt)
    kick = lambda tt: math.sin(2 * math.pi * (55 + 60 * math.exp(-tt * 30)) * tt) * math.exp(-tt * 18)
    snare = lambda tt: (rnd.uniform(-1, 1) * 0.8 + 0.3 * math.sin(2 * math.pi * 190 * tt)) * math.exp(-tt * 22)
    hp = [0.0, 0.0]
    def hat(tt):
        x = rnd.uniform(-1, 1); y = x - hp[0]; z = y - hp[1]; hp[0], hp[1] = x, y   # tilt noise upward
        return z * 0.5 * math.exp(-tt * 70)
    def bassn(f): return lambda tt: math.sin(2 * math.pi * f * tt) * (1 - math.exp(-tt * 200)) * math.exp(-tt * 4)
    def chord(tt):
        s = 0.0
        for f in (220.0, 277.2, 330.0, 440.0):
            ph = (tt * f) % 1.0
            s += 2 * ph - 1          # sawtooth
        return s / 4 * math.exp(-tt * 9)
    truth = {"kick": [], "snare": [], "stab": []}
    for i, b in enumerate(beats):
        bar, pos = divmod(i, 4)
        nb = beats[i + 1] if i + 1 < len(beats) else b + 60 / bpm1
        half = b + (nb - b) / 2
        if pos in (0, 2):                     # guitar stab on the 'and'
            add(half, 0.22, chord, 0.35); truth["stab"].append(half)
        if bar >= 4:
            if pos in (0, 2):
                add(b, 0.25, kick, 0.9); truth["kick"].append(b)
            else:
                add(b, 0.2, snare, 0.55); truth["snare"].append(b)
            add(b, 0.05, hat, 0.12); add(half, 0.05, hat, 0.1)
            f = 55.0 if (bar % 2 == 0) else 73.4
            add(b, (nb - b) / 2 * 0.9, bassn(f), 0.45); add(half, (nb - b) / 2 * 0.9, bassn(f), 0.45)
    peak = max(abs(x) for x in buf) or 1
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(32000 * x / peak)) for x in buf))
    return beats, truth, end
if __name__ == "__main__":
    import json
    beats, truth, end = make(sys.argv[1])
    print(json.dumps({"beats": beats, "truth": truth, "end": end}))
