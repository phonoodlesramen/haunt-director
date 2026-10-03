<p align="center">
  <img src="docs/images/banner.jpg" alt="Haunt Director: free, phone-first show control for your Halloween yard" width="100%">
</p>

<p align="center">
  <b>Turn a Raspberry Pi, a few DMX lights, a fog machine and a speaker into a yard that reacts when trick-or-treaters walk up.</b><br>
  Design scenes on your phone. Arm the show. Go hand out candy.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/hardware.md">What you need</a> ·
  <a href="docs/using-it.md">Tour</a> ·
  <a href="docs/limits-and-unknowns.md">Limits</a> ·
  <a href="docs/safety.md">Safety</a> ·
  <a href="docs/troubleshooting.md">Help</a>
</p>

---

## Hi! 👻

**Haunt Director** is a free, open source show controller for home Halloween displays. It runs on a Raspberry Pi next to [Falcon Player (FPP)](https://github.com/FalconChristmas/fpp), the battle-tested engine the holiday lighting community already trusts, and gives you a friendly, phone-first app on top:

- 🎨 **Scenes you can actually build in an evening.** A timeline editor with lanes for every light, the fogger and your sound. Fades, pulses, strobes, blackouts and fog bursts, previewed live on a picture of your house.
- 🧟 **A show that runs itself.** Arm it at dusk and it rotates ambient scenes with smooth audio crossfades. When the presence sensor spots someone on the path, it fires a scare, then drifts back to ambient after a cooldown. Or go sensor-only: the yard waits dark and silent until someone walks up.
- 🔊 **Real sound, on the Pi.** Music beds, voice lines and sound effects, all mixed together in one engine and kept in sync with the lights. When a voice speaks, the music automatically dips so you can hear it. There's a master volume and Test buttons for sound and fog.
- 🩺 **A System tab that fixes things.** It shows live health for FPP, the lights, sound, sensor and the Pi itself, plus a setup checklist where most problems have a one-click **Fix**. Restart and reboot buttons work from your phone in the yard.
- 🛡️ **Safety rails built in.** Fog bursts can't outlive their cap, and a single master switch keeps the fogger at zero. If the app stops, the lights go dark within a second instead of freezing on. There's an optional limit on how fast strobes can flash, plus a big red **STOP EVERYTHING** button.
- ✨ **Optional extras.** Describe a scene in words and let an AI draft it (bring your own key), or sync lights to the beat of a song.

All of it for $0. No subscription, no cloud account, no login. Just your Pi, on your network.

<p align="center">
  <img src="docs/images/stage-zombie-horde.jpg" alt="The Scenes tab: a scene playing on the stage simulator, with the scene list beside it" width="88%">
</p>

<p align="center">
  <img src="docs/images/phone-live.jpg" alt="Live tab on a phone" width="23%">
  <img src="docs/images/phone-show.jpg" alt="Show and Output settings on a phone" width="23%">
  <img src="docs/images/phone-system.jpg" alt="System tab with FPP health" width="23%">
  <img src="docs/images/phone-library.jpg" alt="Audio library on a phone" width="23%">
</p>

## Why does this exist?

FPP is brilliant at turning channel data into light. But the classic workflow (sequence everything in xLights on a desktop, export, upload, schedule) is built for big Christmas shows choreographed to music. A Halloween yard wants something different: a handful of fixtures, moody loops, sudden scares when someone walks up, and changes you can make on the porch at 7 pm.

Haunt Director keeps FPP doing what it's best at (driving the hardware) and handles the show logic, timing, sound and safety itself. The [How it works](docs/how-it-works.md) page goes through the design.

## What you need

The short version (the [full list, wiring and tested gear](docs/hardware.md) has the details):

| Need | We used | Notes |
|---|---|---|
| Raspberry Pi | Pi 5, 8 GB | Other models untested ([unknowns](docs/limits-and-unknowns.md)) |
| Falcon Player | FPP 9.5 on Raspberry Pi OS Bookworm | Installed first; Haunt Director runs beside it |
| USB to DMX adapter | ENTTEC Open DMX USB (FTDI) | Anything FPP drives as `DMX-Open` or `DMX-Pro` should work |
| DMX lights | 3 × HOLDLAMP IP65 RGBW PARs (8 channel mode) | Other fixtures need a small [profile file](docs/fixture-profiles.md) |
| Fog machine *(optional)* | A 1-channel DMX fogger | Capped, self-expiring bursts |
| Sound *(optional)* | Griffin iMic USB adapter → powered speaker | The Pi 5 has no headphone jack |
| Presence sensor *(optional)* | HLK-LD2410B 24 GHz mmWave | Any sensor with a 3.3 V digital output |

## Quick start

> You'll need a Pi that already runs FPP. If you haven't set that up yet, start with [Install](docs/install.md#1-install-fpp). It's about 20 minutes.

```bash
# on the Pi, as your normal user
git clone https://github.com/phonoodlesramen/haunt-director.git
cd haunt-director
./deploy/install.sh
```

Then open **`http://<your-pi>:8000`** on your phone and:

1. 🩺 Go to the **System** tab. Anything red or yellow has a **Fix** button or a hint. Tap until it says *"Your rig is ready to haunt."*
2. 💡 On **Live**, try a color on each light. If the wrong light responds, fix the addresses (see [Install, step 5](docs/install.md#5-tell-it-about-your-rig)).
3. 🔊 On **Show → Output**, press **Test** for sound and for fog.
4. 🎬 Play an example scene from **Scenes**, then press ✎ to see how it's built.
5. 🎃 Arm the show. Walk past your sensor. Scream a little.

The full walkthrough is in [docs/install.md](docs/install.md).

## The docs

| | |
|---|---|
| 🧰 [What you need](docs/hardware.md) | Equipment, the exact gear it was tested with, wiring, DMX addressing |
| 🛠️ [Install](docs/install.md) | FPP, Haunt Director, first-run setup, updating |
| 🗺️ [Tour](docs/using-it.md) | Every tab, the editor, show mode, sound, the System tab |
| 💡 [Fixture profiles](docs/fixture-profiles.md) | Teach it a new light in one small JSON file |
| ⚙️ [How it works](docs/how-it-works.md) | The output bus, DDP, the audio engine, the safety layers |
| 📏 [Limits and unknowns](docs/limits-and-unknowns.md) | Recommended limits, what's untested, known quirks |
| 🦺 [Safety](docs/safety.md) | Strobes and photosensitivity, fog, outdoor power |
| 🩹 [Troubleshooting](docs/troubleshooting.md) | Real problems we hit and how they were fixed |
| 🔐 [Security](SECURITY.md) | Trusted network only. Why, and how to reach it remotely |
| 🤝 [Contributing](CONTRIBUTING.md) | Dev setup, project layout, wanted: your fixture profiles |

## Honest status

This is version **0.1**, built for one real front yard and tested hard on that one rig in the weeks before Halloween 2026. It works well there. Your Pi, fixtures and Wi-Fi will be different, so expect some rough edges. The [limits and unknowns](docs/limits-and-unknowns.md) page lists what is and isn't proven, and [issues](https://github.com/phonoodlesramen/haunt-director/issues) are very welcome.

## A note on audio

The repo ships with five placeholder sounds that were synthesized from scratch for this project and are dedicated to the public domain (CC0). Bring your own scary audio for the real show. The Library can import from YouTube (via [yt-dlp](https://github.com/yt-dlp/yt-dlp)), but **only use audio you have the right to use**. Lots of free, properly licensed horror sound effects are out there.

## Thanks

To the [Falcon Player](https://github.com/FalconChristmas/fpp) developers and community, whose work does the heavy lifting here. Haunt Director isn't affiliated with or endorsed by the FPP project, and their support channels aren't the place for Haunt Director questions. Please [open an issue here](https://github.com/phonoodlesramen/haunt-director/issues) instead.

## License

[MIT](LICENSE). Use it, fork it, scare people with it. 🎃
