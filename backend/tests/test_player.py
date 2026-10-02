"""Player lifecycle: one scene at a time, newest request wins, audio preloaded
off the event loop, clean natural end. (Guards the orphaned-writer race: two
overlapping play() calls used to leave a second writer that Stop couldn't reach.)"""
import asyncio
import threading
import time

import pytest

from haunt_director.output import OutputBus
from haunt_director.player import ShowPlayer
from haunt_director.domain.scenes import Scene
from haunt_director.store import Store


class NullTransport:
    name = "null"
    def open(self): pass
    def send(self, f, now): return 1
    def close(self, z=None): pass


class Ctrl:
    def __init__(self, store): self.store = store


class SlowAudio:
    """Records calls; preload blocks a worker thread like a cold SD-card read."""
    def __init__(self, delay=0.0):
        self.delay, self.calls, self.preload_threads = delay, [], []
    def preload(self, files, pin=False):
        self.preload_threads.append(threading.current_thread().name)
        time.sleep(self.delay)
    def __getattr__(self, name):
        def rec(*a, **k): self.calls.append(name)
        return rec


def _mk(store, audio=None):
    bus = OutputBus(store, NullTransport())
    return ShowPlayer(Ctrl(store), bus, audio=audio), bus


def _short(store, sid, dur):
    sc = store.scenes[sid].model_copy(deep=True)
    sc.duration_s = dur
    return sc


@pytest.fixture(scope="module")
def store():
    return Store()


def test_overlapping_plays_leave_exactly_one_scene(store):
    async def go():
        p, bus = _mk(store)
        a, b, c = (store.scenes[s] for s in ("blood-moon", "zombie-horde", "lightning-storm"))
        await asyncio.gather(p.play(a), p.play(b), p.play(c))
        live = [t for t in asyncio.all_tasks() if (t.get_name() or "").startswith("cues:")]
        assert len(live) == 1 and live[0].get_name() == "cues:lightning-storm"
        assert p.status["scene"] == "lightning-storm"
        await p.stop()
        assert not p.is_playing and bus.compose(time.monotonic()) == bytes(64)
    asyncio.run(go())


def test_stop_during_preload_wins(store):
    async def go():
        audio = SlowAudio(delay=0.3)
        p, bus = _mk(store, audio)
        sc = store.scenes["blood-moon"]
        assert sc.audio is not None
        t = asyncio.create_task(p.play(sc))
        await asyncio.sleep(0.05)                     # play() is mid-preload
        await p.stop()
        await t
        assert not p.is_playing and p.status["scene"] is None
        assert audio.preload_threads and audio.preload_threads[0] != threading.main_thread().name
    asyncio.run(go())


def test_event_loop_stays_responsive_during_preload(store):
    async def go():
        p, bus = _mk(store, SlowAudio(delay=0.3))
        worst = 0.0
        task = asyncio.create_task(p.play(store.scenes["zombie-horde"]))
        while not task.done():
            t = time.perf_counter(); await asyncio.sleep(0.005)
            worst = max(worst, time.perf_counter() - t - 0.005)
        assert worst < 0.05                           # preload never blocked the loop
        await p.stop()
    asyncio.run(go())


def test_natural_end_holds_last_frame_and_frees_player(store):
    async def go():
        p, bus = _mk(store)
        sc = _short(store, "blood-moon", 0.15)
        await p.play(sc, sustain_on_end=True)
        await asyncio.sleep(0.35)
        assert not p.is_playing
        f = bus.compose(time.monotonic())
        assert f[48] == 0                             # fog never held on
        assert any(f[0:40])                           # the look is held (not dark)
    asyncio.run(go())


def test_seek_moves_the_light_clock(store):
    async def go():
        p, bus = _mk(store)
        await p.play(store.scenes["blood-moon"])
        p.seek(40.0)
        assert abs(p._t - 40.0) < 0.05
        await p.stop()
    asyncio.run(go())


def test_loop_scene_keeps_running(store):
    async def go():
        p, bus = _mk(store)
        await p.play(_short(store, "zombie-horde", 0.1), loop=True)
        await asyncio.sleep(0.35)
        assert p.is_playing and 0.0 <= p._t < 0.1
        await p.stop()
    asyncio.run(go())
