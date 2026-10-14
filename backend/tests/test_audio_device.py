"""Sound output on a Pi with a desktop: ALSA's "default" is the desktop's sound
server or the HDMI port (which fails with no monitor), and a desktop login
removes ~/.asoundrc. So the app opens a USB adapter directly, waits a little for
one that's busy at boot, and never stalls when there's no adapter at all."""
import os
import time

import haunt_director.audio as A

CARDS = """ 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 1 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1
                      vc4-hdmi-1
 2 [system         ]: USB-Audio - iMic USB audio system
                      Griffin Technology, Inc iMic USB audio system at usb-xhci-hcd.0-1, full speed
"""


import pytest


@pytest.fixture(autouse=True)
def _keep_audiodev():
    before = os.environ.get("AUDIODEV")
    yield
    if before is None:
        os.environ.pop("AUDIODEV", None)
    else:
        os.environ["AUDIODEV"] = before


class _Mixer:
    def __init__(self, ok_when, busy_rounds=0):
        self.ok_when, self.busy_rounds, self.calls, self.up = ok_when, busy_rounds, [], False
    def quit(self): self.up = False
    def get_init(self): return (44100, -16, 2) if self.up else None
    def init(self, devicename=None, **kw):
        alsa = os.environ.get("AUDIODEV")
        self.calls.append((devicename, alsa))
        if self.ok_when(devicename, alsa) and self.busy_rounds <= 0:
            self.up = True
            return
        if self.ok_when(devicename, alsa):
            self.busy_rounds -= 1
        raise RuntimeError("ALSA: Couldn't open audio device: Unknown error 524")
    def set_num_channels(self, n): pass
    def set_reserved(self, n): pass
    def Channel(self, i): return object()


class _PG:
    def __init__(self, mixer): self.mixer = mixer


def _engine(tmp_path, monkeypatch, cards, mixer):
    p = tmp_path / "cards"; p.write_text(cards)
    monkeypatch.setattr(A, "ASOUND_CARDS", str(p))
    monkeypatch.setattr(A, "_load_pygame", lambda: _PG(mixer))
    monkeypatch.setattr(A.AudioEngine, "list_devices", staticmethod(lambda: []))   # SDL can't enumerate at boot
    monkeypatch.setattr(A, "BOOT_WAIT_S", 0.01)
    monkeypatch.delenv("AUDIODEV", raising=False)
    return A.AudioEngine(tmp_path / "audio")


def test_the_usb_adapter_is_found_in_alsa():
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(CARDS)
    assert A.usb_alsa_cards(f.name) == [("system", "iMic USB audio system")]
    assert A.usb_alsa_cards("/no/such/file") == []


def test_it_opens_the_usb_adapter_directly_when_default_is_hdmi(tmp_path, monkeypatch):
    mixer = _Mixer(lambda name, alsa: alsa == "plughw:CARD=system,DEV=0")
    eng = _engine(tmp_path, monkeypatch, CARDS, mixer)
    assert eng.available and eng.debug_state()["device"] == "iMic USB audio system"
    assert mixer.calls[0] == (None, "plughw:CARD=system,DEV=0")       # USB first, before "default"
    assert os.environ["AUDIODEV"] == "plughw:CARD=system,DEV=0"


def test_a_usb_adapter_busy_at_boot_gets_a_few_more_tries(tmp_path, monkeypatch):
    mixer = _Mixer(lambda name, alsa: alsa == "plughw:CARD=system,DEV=0", busy_rounds=3)
    eng = _engine(tmp_path, monkeypatch, CARDS, mixer)
    assert eng.available
    assert sum(1 for c in mixer.calls if c[1]) == 4                     # 3 busy, then open


def test_no_adapter_and_no_sound_gives_up_at_once(tmp_path, monkeypatch):
    mixer = _Mixer(lambda name, alsa: False)
    t0 = time.monotonic()
    eng = _engine(tmp_path, monkeypatch, " 0 [vc4hdmi0 ]: vc4-hdmi - vc4-hdmi-0\n", mixer)
    assert not eng.available and time.monotonic() - t0 < 1.0
    assert mixer.calls == [(None, None)]                                # just "default", once
    assert "AUDIODEV" not in os.environ


def test_a_named_device_in_settings_still_comes_first(tmp_path, monkeypatch):
    mixer = _Mixer(lambda name, alsa: name == "My DAC")
    p = tmp_path / "cards"; p.write_text(CARDS)
    monkeypatch.setattr(A, "ASOUND_CARDS", str(p))
    monkeypatch.setattr(A, "_load_pygame", lambda: _PG(mixer))
    monkeypatch.setattr(A.AudioEngine, "list_devices", staticmethod(lambda: []))
    eng = A.AudioEngine(tmp_path / "audio", device="My DAC")
    assert eng.available and mixer.calls[0] == ("My DAC", None) and eng.debug_state()["device"] == "My DAC"
