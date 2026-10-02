# 📏 Limits and unknowns

Here's what's actually been proven, what we'd recommend, and what nobody has tried yet. If you try one of the unknowns, please [tell us how it went](https://github.com/phonoodlesramen/haunt-director/issues). Even a "works fine on my Pi 4" helps.

## Recommended limits

"Tested" means it ran on the [reference rig](hardware.md#the-reference-rig-exactly-what-this-was-tested-with). "Recommended" is where we'd stay for a show you care about.

| Thing | Tested | Recommended | Notes |
|---|---|---|---|
| DMX devices | 4 (3 PARs + fogger) | A handful to a couple dozen | One universe. Beyond about 10 fixtures, nobody has tried it yet |
| Channel block | 1 to 64 | Up to 512 (one universe) | Set `HD_DMX_CHANNEL_COUNT`; the output frame and FPP overlay model follow it. Keep FPP's DMX output count to match. Only 64 has been tested |
| Frame rate | 25 fps | 25 fps | `HD_OUTPUT_FPS`. Fades look smooth at 25, and higher just means more USB traffic |
| Scene length | up to 180 s | 30 to 180 s | Ambient scenes loop the rotation, so long isn't necessary |
| Events per scene | about 400 (*Seance*) | a few hundred | The AI generator caps itself at 120 (beat sync allows more) |
| Scenes | 25 | as many as you like | They're small JSON files |
| Strobe rate | up to ~8 flashes/s (the reference PAR's maximum) | **4 or less** for a public-facing yard | `HD_STROBE_MAX_HZ` caps everything. See [Safety](safety.md#strobes-and-photosensitivity) |
| Fog burst | 8 s cap | 2 to 6 s bursts, with rests between | `HD_FOG_MAX_SECONDS`. Follow your fogger's duty cycle. The AI generator caps itself at 6 s bursts and 6 per scene |
| Sounds playing at once | music + voice + several layers | up to 13 layers | 16 mixer channels in total |
| Audio library | about 65 files, 1.6 GB | whatever fits on the card | Very long files take memory once decoded (roughly 10 MB per stereo minute) |
| Sensor range | LD2410B: about 6 m maximum by spec | aim it along the approach path | Configure range and sensitivity with the vendor's app |
| Scare cooldown | 20 to 30 s | 20 to 60 s | Long enough that one lingering visitor doesn't trigger a loop of scares |
| Devices with the app open | a phone and a laptop at once | a few | Every open page polls for status, and nobody has load-tested it |
| Boot to ready | 1 to 3 minutes | n/a | Slower on weak Wi-Fi. See [Troubleshooting](troubleshooting.md#the-app-says-fpp-offline-after-a-reboot) |

## Unknowns

Honest list. Nobody has tried any of these with Haunt Director yet:

**Hardware**

- **Raspberry Pi 4, 3, Zero 2 W, Compute Modules.** The Pi 4 will probably be fine. Smaller boards may struggle with audio decoding and the web UI at the same time.
- **Other USB DMX adapters** (DMX-Pro style, DMXking, isolated adapters). If FPP supports it, it should work, since Haunt Director never talks to the adapter directly.
- **Other fixtures.** They need a [profile](fixture-profiles.md). Moving heads, pan and tilt, gobos and color wheels have no event types yet.
- **Pixels** (WS2811 and friends). Haunt Director doesn't sequence pixels. FPP can still run them on *other* channels alongside, but that combination is untested.
- **Wireless DMX, Art-Net or E1.31 out to other controllers.** FPP can do it. Untested with Haunt Director.
- **I²S audio HATs and Bluetooth speakers.** HATs that show up as an ALSA card will probably work. Bluetooth adds latency that varies between reconnects, which matters for scares timed to lights.
- **Other presence sensors** (PIR, beam breaks, pressure mats). Anything with an active-high (or active-low, there's a setting) 3.3 V output should work. Only one sensor input is supported.

**Software**

- **FPP 10** and newer. FPP 10 moved to Debian 13, changed its audio stack, and had Pi 5 GPIO trouble in its first release. Haunt Director does its own audio and GPIO, so it may be unaffected, but nobody has checked.
- **The official FPP SD image** (instead of FPP on Raspberry Pi OS). The default users and their sudo rules differ, which the installer handles, but it hasn't been tried end to end.
- **Raspberry Pi OS 13 (Trixie)** and Python above 3.11.
- **Running FPP's own sequences or playlists at the same time.** FPP lets network input override sequence data on the same channels, so it should behave if they use separate channels. Untested.
- **Weeks of unattended running.** It's been run for evening-length shows. SD card wear, memory over weeks, and cold or hot nights are all unproven.

## Known quirks

- **YouTube import breaks sometimes.** YouTube changes often. Run `backend/.venv/bin/pip install -U yt-dlp` and restart the app.
- **Browser preview audio isn't the real mix.** When you play a scene from the editor, your phone or laptop can play the sound too, but without the Pi's ducking or master volume. Judge levels on the real speaker.
- **The stage simulator is approximate.** Beams are soft glows painted over a photo. It's for choreography, not color matching.
- **The app's address can move.** If your router hands the Pi a new address, your bookmark stops working. Give the Pi a DHCP reservation.
- **No user accounts.** Anyone on your network can use it. See [Security](../SECURITY.md).
- **Strobe left on after a scene?** Some fixtures need a few frames with strobe off to actually stop. Set `HD_STROBE_CLEAR_FRAMES=3`.
