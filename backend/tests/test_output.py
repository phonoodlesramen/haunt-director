"""Output bus + transports (output.py).

The headline test replays real scenes, including the transitions that broke on
the rig (Lightning Storm -> Blood Moon, The Tempest -> Zombie Horde), through a
faithful model of FPP 9.5's overlay-range list -- semantics read from the
installed /opt/fpp/src/overlays/PixelOverlay.cpp -- and requires that what FPP
would put on the wire equals the composed frame on EVERY tick.
"""
import socket
import time

import pytest

from haunt_director.output import (DdpTransport, NCH, OutputBus, RestTransport,
                                   TransportError)
from haunt_director.player import FPS, scene_channels
from haunt_director.store import Store


@pytest.fixture(scope="module")
def store():
    return Store()


class NullTransport:
    name = "null"

    def __init__(self):
        self.sent = []

    def open(self):
        pass

    def send(self, frame, now):
        self.sent.append(frame)
        return 1

    def close(self, zero_frame=None):
        if zero_frame is not None:
            self.sent.append(zero_frame)


class FakeFpp:
    """FPP 9.5 overlay ranges: exact (start,end) match updates in place, else
    APPEND; every frame applies the list in order (later wins); only deleteAll
    removes. Also records every range ever written."""

    def __init__(self, fail_at=()):
        self.ranges, self.model_state, self.calls = [], None, 0
        self.fail_at = set(fail_at)
        self.shapes = set()

    def put(self, path, body):
        self.calls += 1
        if self.calls in self.fail_at:
            raise TransportError("injected failure")
        if path == "/overlays/range":
            if body.get("deleteAll"):
                self.ranges.clear()
            return {}
        if path.startswith("/overlays/model/"):
            self.model_state = body.get("State")
            return {}
        spec = path[len("/overlays/range/"):]
        val = body["Value"]
        for part in spec.split(","):
            a, _, b = part.partition("-")
            s, e = int(a) - 1, int(b or a) - 1
            self.shapes.add(e - s + 1)
            for r in self.ranges:
                if r[0] == s and r[1] == e:
                    r[2] = val
                    break
            else:
                self.ranges.append([s, e, val])
        return {}

    def close(self):
        pass

    def wire(self):
        ch = [0] * NCH
        for s, e, v in self.ranges:
            for i in range(s, e + 1):
                ch[i] = v & 0xFF
        return bytes(ch)


# ---------------------------------------------------------------- layering
def test_manual_override_sits_on_top_and_releases(store):
    bus = OutputBus(store, NullTransport())
    sc = store.scenes["blood-moon"]
    bus.set_scene(lambda now: scene_channels(store, sc, 5.0))
    base = bus.compose(0.0)
    bus.set_manual("light_skelly", {1: 200, 2: 255, 3: 0, 4: 0})
    over = bus.compose(0.0)
    assert list(over[0:4]) == [200, 255, 0, 0]
    assert over[16:] == base[16:]                      # other fixtures untouched
    bus.release_manual("light_skelly")
    assert bus.compose(0.0) == base


def test_fog_burst_expires_back_to_scene(store):
    bus = OutputBus(store, NullTransport())
    sc = store.scenes["blood-moon"]
    bus.set_scene(lambda now: scene_channels(store, sc, 5.0))
    scene_fog = bus.compose(0.0)[48]
    bus.set_manual("fog_main", {49: 255}, expires_at=10.0)
    assert bus.compose(9.99)[48] == 255
    assert bus.compose(10.0)[48] == scene_fog
    assert bus.manual_devices() == []


def test_fog_guard_beats_scene_and_manual(store):
    bus = OutputBus(store, NullTransport())
    bus.set_scene(lambda now: {49: 200})
    bus.set_manual("fog_main", {49: 255})
    bus.fog_enabled = False
    assert bus.compose(0.0)[48] == 0


def test_natural_end_holds_frame_with_fog_and_strobe_off(store):
    bus = OutputBus(store, NullTransport())
    bus.set_scene(lambda now: {1: 90, 2: 255, 6: 255, 49: 220})
    bus.compose(0.0)
    bus.end_scene(hold=True)
    f = bus.compose(1.0)
    assert (f[0], f[1]) == (90, 255)                   # colour held (same look as before)
    assert f[5] == 0 and f[48] == 0                    # strobe off, fog off: never held on


def test_blackout_clears_everything(store):
    bus = OutputBus(store, NullTransport())
    bus.set_scene(lambda now: {1: 90, 49: 200})
    bus.set_manual("light_path", {17: 255})
    bus.blackout()
    assert bus.compose(0.0) == bytes(NCH)


def test_bad_scene_source_never_kills_output(store):
    bus = OutputBus(store, NullTransport())
    good = {1: 50}
    bus.set_scene(lambda now: good)
    bus.compose(0.0)
    bus.set_scene(lambda now: 1 / 0)
    assert bus.compose(0.0)[0] == 50                   # keeps last good frame
    assert "scene" in (bus.last_error or "")


# ------------------------------------------- REST vs FPP's range list
def _replay(store, seq, fpp, bus_rest_ticks=None):
    tr = RestTransport(fpp, "haunt")
    bus = OutputBus(store, tr)
    tr.open()
    mismatches, t_clock = 0, 1000.0
    for sid in seq:
        sc = store.scenes[sid]
        n = int(sc.duration_s * FPS) + 1
        start = t_clock
        bus.set_scene(lambda now, sc=sc, start=start: scene_channels(store, sc, min(now - start, sc.duration_s)))
        for i in range(n):
            now = start + i / FPS
            frame = bus.compose(now)
            try:
                tr.send(frame, now)
            except TransportError:
                pass
            if fpp.wire() != frame:
                mismatches += 1
        t_clock = start + n / FPS
        bus.end_scene(hold=True)
    return mismatches


@pytest.mark.parametrize("seq", [["lightning-storm", "blood-moon"],
                                 ["the-tempest", "zombie-horde"]])
def test_rest_never_leaves_stale_ranges_on_reported_transitions(store, seq):
    fpp = FakeFpp()
    assert _replay(store, seq, fpp) == 0
    assert fpp.shapes == {1}                           # only ever single-channel ranges


def test_rest_open_purges_stale_ranges_first(store):
    fpp = FakeFpp()
    fpp.ranges = [[0, 3, 0]]                           # the "1-4 = 0" that killed Skelly
    tr = RestTransport(fpp, "haunt")
    tr.open()
    assert fpp.ranges == [] and fpp.model_state == 1
    frame = bytearray(NCH); frame[0] = 15; frame[1] = 18
    tr.send(bytes(frame), 0.0)
    assert fpp.wire() == bytes(frame)


def test_rest_failed_put_is_resent_next_tick(store):
    fpp = FakeFpp()
    tr = RestTransport(fpp, "haunt")
    tr.open()
    tr.send(bytes(NCH), 0.0)                           # keyframe of zeros
    fpp.fail_at = {fpp.calls + 1}                      # the very next PUT fails
    frame = bytearray(NCH); frame[0] = 200
    with pytest.raises(TransportError):
        tr.send(bytes(frame), 0.04)
    assert fpp.wire()[0] == 0                          # lost on the wire...
    tr.send(bytes(frame), 0.08)
    assert fpp.wire()[0] == 200                        # ...and re-sent next tick


def test_rest_coalesces_equal_values_into_one_request(store):
    fpp = FakeFpp()
    tr = RestTransport(fpp, "haunt")
    tr.open()
    before = fpp.calls
    tr.send(bytes(NCH), 0.0)                           # 64 zeros
    assert fpp.calls - before <= 2                     # (maybe a model re-enable) + ONE range PUT


# ------------------------------------------------------------------- DDP
def test_ddp_packet_matches_fpp_parser():
    pkt = DdpTransport.packet(bytes(range(64)), seq=7)
    assert pkt[0] == 0x41                              # v1 | PUSH
    assert pkt[1] == 7 and pkt[3] == 1                 # sequence, destination id 1
    assert int.from_bytes(pkt[4:8], "big") == 0        # channel offset
    assert int.from_bytes(pkt[8:10], "big") == 64      # length
    assert pkt[10:] == bytes(range(64))


def test_ddp_sends_one_datagram_per_frame():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0)); rx.settimeout(1.0)

    class Http:
        def __init__(self): self.calls, self.active = [], 1
        def put(self, p, b):
            self.calls.append((p, b))
            if p.endswith("/state"): self.active = b["State"]
            return {}
        def get(self, p): return {"isActive": self.active}
        def close(self): pass

    http = Http()
    tr = DdpTransport(http, "haunt", "127.0.0.1", rx.getsockname()[1])
    tr.open()
    assert ("/overlays/range", {"deleteAll": True}) in http.calls
    assert ("/overlays/model/haunt/state", {"State": 0}) in http.calls   # model can't mask DDP
    frame = bytes([9] * 64)
    tr.send(frame, time.monotonic())
    data, _ = rx.recvfrom(2048)
    assert len(data) == 10 + 64 and data[10:] == frame
    # never re-send State:0 to an already-disabled model (FPP 9.5 treats that as "enable")
    n_state = sum(1 for p, _ in http.calls if p.endswith("/state"))
    tr._next_reassert = 0.0
    tr.send(frame, time.monotonic()); rx.recvfrom(2048)
    assert sum(1 for p, _ in http.calls if p.endswith("/state")) == n_state
    tr.close(bytes(64))
    rx.close()


# ------------------------------------------------------------- the clock
def test_bus_thread_holds_its_rate_and_leaves_rig_dark(store):
    tr = NullTransport()
    bus = OutputBus(store, tr, fps=25)
    bus.set_scene(lambda now: {1: 100})
    bus.start()
    time.sleep(1.0)
    stats = bus.stats()
    bus.stop()
    assert 21 <= len(tr.sent) - 1 <= 29                # ~25 frames in 1 s (+1 zero frame)
    assert stats["alive"] and stats["send_errors"] == 0
    assert tr.sent[-1] == bytes(NCH)                   # stop() leaves everything off


def test_global_strobe_cap_limits_every_strobe():
    """HD_STROBE_MAX_HZ: one ceiling for scenes and Live cards (photosensitivity)."""
    from haunt_director.domain import resolve as R
    from haunt_director.store import Store
    st = Store()
    prof = st.profiles["holdlamp-rgbw-8ch"]
    fast = R.resolve_fixture(prof, 1, rgb=(255, 255, 255), brightness=255, strobe_hz=15)
    slow = R.resolve_fixture(prof, 1, rgb=(255, 255, 255), brightness=255, strobe_hz=4)
    try:
        R.set_strobe_cap(4)
        capped = R.resolve_fixture(prof, 1, rgb=(255, 255, 255), brightness=255, strobe_hz=15)
        assert capped == slow != fast
        assert R.capped_strobe_hz(15) == 4 and R.capped_strobe_hz(2) == 2 and not R.capped_strobe_hz(0)
    finally:
        R.set_strobe_cap(0)
    assert R.resolve_fixture(prof, 1, rgb=(255, 255, 255), brightness=255, strobe_hz=15) == fast
