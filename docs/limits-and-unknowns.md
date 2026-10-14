# 📏 Limits and unknowns

Here's what's actually been proven, what we'd recommend, and what nobody has tried yet. If you try one of the unknowns, please [tell us how it went](https://github.com/phonoodlesramen/haunt-director/issues). Even a "works fine on my Pi 4" helps.

## Recommended limits

"Tested" means it ran on the [reference rig](hardware.md#the-reference-rig-exactly-what-this-was-tested-with). "Recommended" is where we'd stay for a show you care about.

| Thing | Tested | Recommended | Notes |
|---|---|---|---|
| DMX devices | 4 (3 PARs + fogger) | A handful to a couple dozen | One universe. Beyond about 10 fixtures, nobody has tried it yet |
| Channel block | 1 to 64 | Up to 512 (one universe) | Set `HD_DMX_CHANNEL_COUNT`; the output frame and FPP overlay model follow it. Keep FPP's DMX output count to match. Only 64 has been tested |
| Frame rate | 25 fps | 25 fps | `HD_OUTPUT_FPS`. Fades look smooth at 25, and higher just means more USB traffic |
| Scene length | up to 300 s | 30 to 300 s | The generator and Extend stop at 5 minutes. Ambient scenes loop the rotation, so long isn't required |
| Events per scene | about 2,200 (a 5-minute beat-sync test scene) | a few hundred to about 2,500 | The AI generator caps itself at 120, or 1.5 per second for longer scenes. Beat sync and song shows allow about 7 per second for each light (6,000 at most), and thin out evenly if they'd go over, so the ending stays. A song show on 3 lights is about 1,500 for a 3 1/2-minute song. A 2,000-event scene opens in the editor in about 3 seconds on a slow phone |
| Scenes | 25 | as many as you like | They're small JSON files |
| Strobe rate | up to ~8 flashes/s (the reference PAR's maximum) | **4 or less** for a public-facing yard | `HD_STROBE_MAX_HZ` caps everything. See [Safety](safety.md#strobes-and-photosensitivity) |
| Fog burst | 8 s cap | 2 to 6 s bursts, with rests between | `HD_FOG_MAX_SECONDS`. Follow your fogger's duty cycle. The AI generator caps itself at 6 s bursts, at least 10 s apart, and 6 per scene (1 per 30 s for scenes over 3 minutes) |
| Sounds playing at once | music + voice + several layers | up to 13 layers | 16 mixer channels in total |
| Audio library | about 65 files, 1.6 GB | whatever fits on the card | Very long files take memory once decoded (roughly 10 MB per stereo minute) |
| Sensor range | LD2410B: about 6 m maximum by spec | aim it along the approach path | Configure range and sensitivity with the vendor's app |
| Scare cooldown | 20 to 30 s | 20 to 60 s | Long enough that one lingering visitor doesn't trigger a loop of scares |
| Devices with the app open | a phone and a laptop at once | a few | Every open page polls for status, and nobody has load-tested it |
| Boot to ready | 1 to 3 minutes | n/a | Slower on weak Wi-Fi. See [Troubleshooting](troubleshooting.md#the-app-says-fpp-offline-after-a-reboot) |

## Unknowns

Honest list. Nobody has tried any of these with Haunt Director yet:

**Hardware**

- **Raspberry Pi 5 with 4 GB or less, Pi 4, 3, Zero 2 W, Compute Modules.** The whole stack (FPP, Haunt Director and the desktop) uses about 1 GB of RAM on the reference rig, so a 4 GB Pi 5 or a Pi 4 will probably be fine. Smaller boards may struggle with audio decoding and the web UI at the same time.
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
- **Weeks of unattended running.** It's been run for evening-length shows, and the reference Pi has stayed up for over a week between restarts. SD card wear, memory over weeks, and cold or hot nights (December, we're looking at you) are all unproven.

## Known quirks

- **Song shows have been tuned on one song so far** (a 4/4 rock song at about 115 BPM). Expect other genres to need a tweak in the editor, and songs without a clear drum beat to be refused.
- **yt-dlp may warn about a JavaScript runtime.** Newer yt-dlp wants [deno](https://deno.com) for YouTube. Downloads still work without it for now, but some formats may be missing; installing deno on the Pi silences the warning.
- **YouTube import breaks sometimes.** YouTube changes often. Run `backend/.venv/bin/pip install -U yt-dlp` and restart the app.
- **Browser preview audio isn't the real mix.** When you play a scene from the editor, your phone or laptop can play the sound too, but without the Pi's ducking or master volume. Judge levels on the real speaker.
- **The stage simulator is approximate.** Beams are soft glows painted over a photo. It's for choreography, not color matching.
- **The app's address can move.** If your router hands the Pi a new address, your bookmark stops working. Give the Pi a DHCP reservation.
- **No user accounts.** Anyone on your network can use it. See [Security](../SECURITY.md).
- **Strobe left on after a scene?** Some fixtures need a few frames with strobe off to actually stop. Set `HD_STROBE_CLEAR_FRAMES=3`.
