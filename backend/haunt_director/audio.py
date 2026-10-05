"""
Pi-side audio for Haunt Director.

Design goals (reliability first):
  * A broken or missing audio device must NEVER stop the light show — every
    entry point is guarded and degrades to a silent no-op.
  * Music bed + a voice-over + layered SFX must be able to play AT THE SAME
    TIME, so we use a single in-process mixer (pygame) rather than several ALSA
    clients that would need dmix. One process, one device, internal mixing.
  * Scene MUSIC plays on reserved mixer *channels* (not pygame's single
    streaming mixer.music slot) so two beds can overlap for a crossfade: when a
    new scene starts, the outgoing scene's audio fades out on its own channel
    while the incoming scene plays on a fresh one.
  * All source audio is normalised to 44.1kHz stereo WAV in a cache via ffmpeg,
    so we never depend on the SDL_mixer build having an mp3/other decoder.
  * Voice ducking: while a voice is speaking (the scene voice-over, or a layer
    the library classes as a voice) the music bed and looping ambience dip by
    a set number of dB, then come back. One-shot SFX are never ducked, so a
    scream or thunder crack keeps its punch. A small envelope thread does it.

Channel map (16 channels, first 3 reserved so find_channel() never steals them):
    0, 1 -> music A / B  (ping-pong so a crossfade has two live beds)
    2    -> voice-over
    3..15 -> layered beds / sfx cues (auto-allocated)

Public API (all safe to call even when audio is unavailable):
    eng = AudioEngine(audio_dir, device=None)
    eng.available            -> bool
    eng.play_music(fname, gain=1.0, loop=True)
    eng.play_voiceover(fname, gain=1.0)
    eng.play_cue(fname, gain=1.0, loop=False, key=None)
    eng.fade_out_current(ms)     # crossfade helper: fade everything playing now
    eng.set_duck_db(db)          # voice ducking depth (0 = off)
    eng.stop()                   # hard cut
    AudioEngine.list_devices()   -> list[str]   (SDL playback device names)
"""
from __future__ import annotations
import collections
import contextlib
import logging, os, shutil, subprocess, threading
from pathlib import Path

log = logging.getLogger("haunt.audio")

# pygame's import must be serialised: importing it from two threads at once
# (audio-init thread + a /api/audio/devices call) deadlocks Python's import lock.
_PYGAME_LOCK = threading.Lock()
def _load_pygame():
    with _PYGAME_LOCK:
        import pygame
        import pygame.mixer  # noqa: F401
        try:
            import pygame._sdl2.audio  # noqa: F401
        except Exception:
            pass
        return pygame

# substrings that identify a USB DAC / adapter in SDL/ALSA device names
_USB_HINTS = ("usb", "uac", "audio adapter", "headset", "dac", "device")

# voice ducking envelope (linear in dB)
DUCK_ATTACK_DB_S = 60.0    # dip fast: 9 dB in 0.15 s, so the first words are clear
DUCK_RELEASE_DB_S = 12.0   # recover gently: 9 dB in 0.75 s
DUCK_HOLD_S = 0.4          # bridge short gaps so back-to-back lines don't pump
DUCK_TICK_S = 0.025
DUCK_MAX_DB = 24.0

_MUSIC_A = 0        # reserved channel indices
_MUSIC_B = 1
_VO_CH = 2
_RESERVED = 3       # channels 0,1,2 are reserved; cues auto-allocate from 3+


class AudioEngine:
    def __init__(self, audio_dir, device: str | None = None, cache_dir=None):
        self.audio_dir = Path(audio_dir)
        self.device = device                      # explicit SDL device name, or None to auto/default
        self.cache_dir = Path(cache_dir) if cache_dir else (self.audio_dir / "_wavcache")
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._ok = False
        self._pg = None
        self._music_slots = []                     # [Channel(0), Channel(1)]
        self._music_i = 0                          # which slot currently holds live music
        self._music = None                         # (channel, sound, gain) or None
        self._vo = None                            # dedicated voice-over channel
        self._music_gain = 1.0
        self._vo_gain = 1.0
        # App master volume (0..1 amplitude). Applied at CHANNEL level only, so it
        # scales the whole mix without changing any scene's internal balance.
        self._master = 1.0
        self._cue = {}          # key -> (channel, sound, gain) for live control
        # wavpath -> pygame Sound, LRU. A Sound can play on many channels at once.
        # Pinned entries (scare audio) are never evicted so a trigger hits instantly.
        self._sndcache = collections.OrderedDict()
        self._pinned = set()
        self._cache_cap = 16    # unpinned Sounds kept decoded
        self._mute = set()      # lane keys currently silenced
        # voice ducking: cue key -> "voice" | "bed" | "sfx"; beds and music duck
        # while any voice plays. _duck_db is the live envelope (<= 0 dB).
        self._cue_role = {}
        self._duck_depth_db = 0.0
        self._duck_db = 0.0
        self._voice_seen = -1e9
        self._duck_last = None
        self._duck_thread = None
        self._lock = threading.RLock()
        self._init_mixer()

    # ---- setup ----
    def _init_mixer(self):
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")   # headless: no video
        os.environ.setdefault("SDL_AUDIODRIVER", "alsa")
        try:
            self._pg = _load_pygame()
        except Exception as e:
            log.warning("audio disabled (pygame import failed: %s)", e)
            return
        dev = self.device or self._auto_device()
        kw = dict(frequency=44100, size=-16, channels=2, buffer=1024)
        for attempt in ([dev] if dev else []) + [None]:
            try:
                self._pg.mixer.quit()
            except Exception:
                pass
            try:
                if attempt:
                    self._pg.mixer.init(devicename=attempt, **kw)
                else:
                    self._pg.mixer.init(**kw)
                self._pg.mixer.set_num_channels(16)
                with contextlib.suppress(Exception):
                    self._pg.mixer.set_reserved(_RESERVED)   # keep music+vo away from find_channel()
                self._music_slots = [self._pg.mixer.Channel(_MUSIC_A), self._pg.mixer.Channel(_MUSIC_B)]
                self._music_i = 0
                self._music = None
                self._vo = self._pg.mixer.Channel(_VO_CH)
                self._sndcache.clear()
                self._cue.clear()                      # old channels are gone with the old mixer
                self._cue_role.clear()
                self._ok = True
                self.device = attempt
                log.info("audio ready (device=%s)", attempt or "default")
                self._start_duck_thread()
                return
            except Exception as e:
                log.warning("audio init failed on device=%s: %s", attempt or "default", e)
        self._ok = False

    def _auto_device(self):
        for name in self.list_devices():
            if any(h in name.lower() for h in _USB_HINTS):
                return name
        return None

    @staticmethod
    def list_devices():
        # Enumerate SDL playback devices WITHOUT tearing down a running mixer.
        # (pygame.quit() here would kill the engine's live mixer.)
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "alsa")
        try:
            pygame = _load_pygame()
            from pygame._sdl2 import audio as sdl2_audio
            running = pygame.mixer.get_init() is not None
            if running:
                # SDL audio is already up (mixer is live) — just read the names.
                return list(sdl2_audio.get_audio_device_names(False))
            # Nothing running: bring the mixer up briefly to enumerate, then drop
            # only the mixer we just created (never a full pygame.quit()).
            pygame.mixer.init()
            try:
                return list(sdl2_audio.get_audio_device_names(False))
            finally:
                try: pygame.mixer.quit()
                except Exception: pass
        except Exception as e:
            log.warning("could not enumerate audio devices: %s", e)
            return []

    @property
    def available(self) -> bool:
        return self._ok

    # ---- source normalisation ----
    def _wav(self, filename: str):
        src = self.audio_dir / filename
        if not src.exists():
            log.warning("audio file missing: %s", src)
            return None
        # Always normalise to standard 44.1k/16-bit/stereo PCM WAV in the cache.
        # Some .wav exports (AI music tools, DAWs) carry a header SDL_mixer rejects ("Unknown
        # WAVE format"), so a raw .wav cannot be trusted — convert everything.
        dst = self.cache_dir / (src.stem + ".norm.wav")
        try:
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                return dst
        except Exception:
            pass
        ff = shutil.which("ffmpeg")
        if not ff:
            log.warning("ffmpeg missing; using %s as-is", filename)
            return src if src.suffix.lower() == ".wav" else None
        try:
            subprocess.run([ff, "-y", "-i", str(src), "-ar", "44100", "-ac", "2",
                            "-c:a", "pcm_s16le", str(dst)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
            return dst
        except Exception as e:
            log.warning("ffmpeg convert failed for %s: %s", filename, e)
            return src if src.suffix.lower() == ".wav" else None

    def _cache_put(self, key, snd):
        """Insert into the LRU (caller holds self._lock); evict oldest unpinned."""
        self._sndcache[key] = snd
        self._sndcache.move_to_end(key)
        unpinned = [k for k in self._sndcache if k not in self._pinned and k != key]
        excess = len(unpinned) + (0 if key in self._pinned else 1) - self._cache_cap
        for k in unpinned[:max(0, excess)]:
            self._sndcache.pop(k, None)          # a playing Channel keeps its own ref

    def _sound(self, wavpath):
        """Cached pygame Sound. One Sound can play on several channels at once,
        which is exactly what a same-file crossfade needs. A miss here decodes
        the file ON THE CALLER'S THREAD -- the player preloads every scene's
        audio off the event loop first, so this should only happen rarely."""
        key = str(wavpath)
        s = self._sndcache.get(key)
        if s is None:
            log.info("audio cache miss (decoding inline): %s", os.path.basename(key))
            s = self._pg.mixer.Sound(key)
            self._cache_put(key, s)
        else:
            self._sndcache.move_to_end(key)
        return s

    def preload(self, filenames, pin: bool = False):
        """Convert (ffmpeg, if needed) and decode every file into the Sound cache.
        BLOCKING by design: call from a worker thread (asyncio.to_thread).
        pygame releases the GIL while loading (measured on the Pi: a 122 ms load
        moved the event loop by 3 ms), so lights and requests keep running."""
        self._ensure_mixer()
        if not self._ok:
            return
        for f in filenames or []:
            if not f:
                continue
            w = self._wav(f)
            if not w:
                continue
            key = str(w)
            with self._lock:
                have = key in self._sndcache
                if have:
                    self._sndcache.move_to_end(key)
                if pin:
                    self._pinned.add(key)
            if have:
                continue
            try:
                snd = self._pg.mixer.Sound(key)      # the slow part -- outside the lock
            except Exception as e:
                log.warning("preload failed for %s: %s", f, e)
                continue
            with self._lock:
                if key not in self._sndcache:
                    self._cache_put(key, snd)

    # ---- playback ----
    def _ensure_mixer(self):
        try:
            if self._pg is not None and self._pg.mixer.get_init() is None:
                log.warning("mixer was down; re-initialising")
                self._init_mixer()
        except Exception as e:
            log.warning("mixer ensure failed: %s", e)

    def play_music(self, filename: str | None, gain: float = 1.0, loop: bool = True):
        self._ensure_mixer()
        if not self._ok or not filename:
            return
        w = self._wav(filename)
        if not w:
            return
        self._music_gain = _clamp(gain)
        with self._lock:
            try:
                ch = self._music_slots[self._music_i]
                with contextlib.suppress(Exception):
                    ch.stop()                       # free this slot (no-op if already idle)
                snd = self._sound(w)
                snd.set_volume(0.0 if "music" in self._mute else self._music_gain)
                ch.play(snd, loops=(-1 if loop else 0))
                ch.set_volume(self._music_vol(self._music_gain))
                self._music = (ch, snd, self._music_gain)
            except Exception as e:
                log.warning("play_music failed: %s", e)

    def play_voiceover(self, filename: str | None, gain: float = 1.0):
        self._ensure_mixer()
        if not self._ok or not filename or not self._vo:
            return
        w = self._wav(filename)
        if not w:
            return
        self._vo_gain = _clamp(gain)
        with self._lock:
            try:
                snd = self._sound(w)
                snd.set_volume(0.0 if "voiceover" in self._mute else self._vo_gain)
                self._vo.play(snd)
                self._vo.set_volume(self._vo_vol())
            except Exception as e:
                log.warning("play_voiceover failed: %s", e)

    def play_cue(self, filename: str | None, gain: float = 1.0, loop: bool = False, key=None,
                 role: str | None = None):
        """Play a layered bed/sfx on an auto-allocated channel (mixes with music+vo).
        role: "voice" (ducks the bed), "bed" (is ducked) or "sfx" (left alone);
        default is "bed" for a loop, "sfx" for a one-shot."""
        self._ensure_mixer()
        if not self._ok or not filename:
            return
        w = self._wav(filename)
        if not w:
            return
        g = _clamp(gain)
        with self._lock:
            try:
                snd = self._sound(w)
                snd.set_volume(0.0 if (key is not None and key in self._mute) else g)
                ch = self._pg.mixer.find_channel(True)   # steal oldest non-reserved if needed
                if ch is not None:
                    if key is not None:
                        self._cue_role[key] = role if role in ("voice", "bed", "sfx") else ("bed" if loop else "sfx")
                    ch.play(snd, loops=(-1 if loop else 0))
                    ch.set_volume(self._cue_vol(key, g))
                    if key is not None:
                        self._cue[key] = (ch, snd, g)
            except Exception as e:
                log.warning("play_cue failed: %s", e)

    def fade_out_current(self, ms: int = 1500):
        """Crossfade helper: fade out everything playing RIGHT NOW (music bed,
        voice-over, layered cues) over `ms`, non-blocking, and detach it so the
        next play_* starts on fresh channels. The light show is untouched.
        Falls back to an immediate stop if a fade can't be scheduled."""
        if not self._ok:
            return
        ms = max(1, int(ms))
        with self._lock:
            # music bed: fade its channel, then ping-pong to the other slot so the
            # incoming bed plays somewhere the fading one isn't.
            try:
                if self._music is not None:
                    ch, snd, g = self._music
                    try:
                        ch.fadeout(ms)
                    except Exception:
                        with contextlib.suppress(Exception): ch.stop()
                    self._music = None
                    self._music_i ^= 1
            except Exception:
                pass
            # voice-over
            try:
                if self._vo is not None:
                    self._vo.fadeout(ms)
            except Exception:
                with contextlib.suppress(Exception):
                    if self._vo is not None: self._vo.stop()
            # layered cues
            for k, (ch, snd, g) in list(self._cue.items()):
                try:
                    ch.fadeout(ms)
                except Exception:
                    with contextlib.suppress(Exception): ch.stop()
            self._cue.clear()
            self._cue_role.clear()

    def stop(self):
        if not self._ok:
            return
        with self._lock:
            try:
                self._pg.mixer.stop()   # stop ALL channels (music slots + vo + every layer)
            except Exception:
                pass
            with contextlib.suppress(Exception):
                self._pg.mixer.music.stop()   # legacy slot (unused now), harmless
            self._cue.clear()
            self._cue_role.clear()
            self._music = None
            self._duck_db = 0.0                 # nothing is playing: no dip to carry over
            self._voice_seen = -1e9

    def set_master(self, gain: float):
        """App master volume, 0..1 amplitude. Takes effect immediately on
        everything already playing (music bed, voice-over, every layer)."""
        self._master = _clamp(gain)
        self.set_mute(self._mute)            # re-applies every live channel volume

    @property
    def master(self) -> float:
        return self._master

    def set_mute(self, mute):
        """Silence/restore lanes on audio that is ALREADY playing (live)."""
        self._mute = set(mute or [])
        if not self._ok:
            return
        with self._lock:
            self._apply_live()

    # ---- effective channel volumes: lane mute x level x master x duck ----
    def _music_vol(self, g):
        return 0.0 if "music" in self._mute else g * self._master * self._duck_gain()

    def _vo_vol(self):
        return 0.0 if "voiceover" in self._mute else self._vo_gain * self._master

    def _cue_vol(self, key, g):
        if key is not None and key in self._mute:
            return 0.0
        duck = self._duck_gain() if self._cue_role.get(key) == "bed" else 1.0
        return g * self._master * duck

    def _apply_live(self):
        """Re-apply every live channel's volume (caller holds self._lock)."""
        try:
            if self._music is not None:
                ch, snd, g = self._music
                ch.set_volume(self._music_vol(g))
        except Exception:
            pass
        try:
            if self._vo is not None:
                self._vo.set_volume(self._vo_vol())
        except Exception:
            pass
        for k, (ch, snd, g) in list(self._cue.items()):
            try:
                if ch is not None and ch.get_sound() is snd:
                    ch.set_volume(self._cue_vol(k, g))
                else:
                    self._cue.pop(k, None)           # channel was reused: stop tracking it
                    self._cue_role.pop(k, None)
            except Exception:
                pass

    # ---- voice ducking ----
    def set_duck_db(self, db):
        """How far music and looping beds dip while a voice plays, in dB (0 = off).
        Takes effect on the next envelope tick, smoothly."""
        try:
            d = float(db)
        except Exception:
            d = 0.0
        self._duck_depth_db = max(0.0, min(DUCK_MAX_DB, d))

    @property
    def duck_depth_db(self) -> float:
        return self._duck_depth_db

    def _duck_gain(self) -> float:
        return 10.0 ** (self._duck_db / 20.0)

    def _voice_active(self) -> bool:
        """Is a (non-muted) voice audible right now? Caller holds self._lock."""
        try:
            if self._vo is not None and "voiceover" not in self._mute and self._vo.get_busy():
                return True
        except Exception:
            pass
        for k, (ch, snd, g) in list(self._cue.items()):
            if self._cue_role.get(k) != "voice" or k in self._mute:
                continue
            try:
                if ch.get_sound() is snd and ch.get_busy():
                    return True
            except Exception:
                pass
        return False

    def _duck_step(self, now: float):
        """Advance the duck envelope one tick and re-apply live volumes if it
        moved. Linear in dB: fast attack, short hold, gentle release."""
        if not self._ok:
            self._duck_last = now
            return
        with self._lock:
            if self._voice_active():
                self._voice_seen = now
            ducking = self._duck_depth_db > 0 and (now - self._voice_seen) < DUCK_HOLD_S
            target = -self._duck_depth_db if ducking else 0.0
            dt = 0.0 if self._duck_last is None else max(0.0, min(0.1, now - self._duck_last))
            self._duck_last = now
            cur = self._duck_db
            if cur > target:
                cur = max(target, cur - DUCK_ATTACK_DB_S * dt)
            elif cur < target:
                cur = min(target, cur + DUCK_RELEASE_DB_S * dt)
            if cur != self._duck_db:
                self._duck_db = cur
                self._apply_live()

    def _start_duck_thread(self):
        t = self._duck_thread
        if t is not None and t.is_alive():
            return
        import time as _time

        def _loop():
            while True:
                try:
                    self._duck_step(_time.monotonic())
                except Exception:
                    pass
                _time.sleep(DUCK_TICK_S)

        self._duck_thread = threading.Thread(target=_loop, name="audio-duck", daemon=True)
        self._duck_thread.start()

    def debug_state(self):
        """Live snapshot of what the mixer is actually playing (diagnostics)."""
        if not self._ok:
            return {"ok": False}
        out = {"ok": True, "device": self.device, "mute": sorted(self._mute),
               "master": round(self._master, 3),
               "duck": {"depth_db": self._duck_depth_db, "now_db": round(self._duck_db, 1),
                        "thread": bool(self._duck_thread is not None and self._duck_thread.is_alive())}}
        with self._lock:
            try:
                if self._music is not None:
                    ch, snd, g = self._music
                    out["music_busy"] = bool(ch.get_busy())
                    out["music_vol"] = round(ch.get_volume(), 2)
                else:
                    out["music_busy"] = False
            except Exception:
                out["music_busy"] = None
            try: out["vo"] = {"busy": bool(self._vo.get_busy()), "vol": round(self._vo.get_volume(), 2)}
            except Exception: out["vo"] = {}
            cues = {}
            for k, (ch, snd, g) in list(self._cue.items()):
                try: cues[k] = {"busy": bool(ch.get_busy()), "vol": round(ch.get_volume(), 2),
                                "role": self._cue_role.get(k)}
                except Exception: cues[k] = {"err": True}
            out["cues"] = cues
            try:
                out["active_channels"] = sum(
                    1 for i in range(self._pg.mixer.get_num_channels())
                    if self._pg.mixer.Channel(i).get_busy())
            except Exception:
                pass
        return out

    def warm_cache(self, filenames):
        """Pre-convert a set of files so the first play has no ffmpeg hitch."""
        for f in filenames:
            if f:
                self._wav(f)

    def prune_cache(self) -> int:
        """Delete converted copies whose source clip is gone from the audio
        folder (a 5-minute song's copy is ~50 MB). Only touches *.norm.wav
        files in the cache folder, and does nothing if the audio folder can't
        be read. Returns how many were removed."""
        try:
            stems = {p.stem for p in self.audio_dir.iterdir() if p.is_file()}
            cached = list(self.cache_dir.glob("*.norm.wav"))
        except Exception:
            return 0
        if not stems:
            return 0                                # an empty or unmounted folder: leave the cache alone
        gone = 0
        for c in cached:
            if c.name[:-len(".norm.wav")] not in stems:
                with contextlib.suppress(Exception):
                    c.unlink()
                    gone += 1
        if gone:
            log.info("wav cache: removed %d orphaned copies", gone)
        return gone


def _clamp(v, lo=0.0, hi=1.0):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return 1.0
