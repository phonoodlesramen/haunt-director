# 🧰 What you need

Haunt Director is deliberately small: one Pi, one DMX cable, a handful of fixtures. Here's what to buy for the show you want, the exact gear it was tested with, and how it all plugs together. The same gear does Halloween, Christmas and everything in between; only the colors (and the sound effects) change.

> **Legend:** ✅ tested on the reference rig · 🤞 should work, untested · ❌ known not to work

## Pick a tier

You don't have to buy it all at once. Each tier keeps everything from the one before it.

| Tier | Add this | You get | Roughly |
|---|---|---|---|
| 🧪 **Test drive** | Nothing. Run it on a laptop ([how](../CONTRIBUTING.md#development-setup)) | Scenes, the editor, the house simulator, the sound library | **$0** |
| 🕯️ **Porch Light** | Pi, power supply, case, microSD, USB to DMX adapter, DMX cables and a terminator, four indoor RGBW PARs | House lights on a schedule, Live control, full light scenes. Keep indoor lights under cover: the porch, the eaves, or inside the front windows shining out | **about $325** |
| 🎃 **Front Yard Fright** | Swap to outdoor IP65 PARs, then add a DMX fogger, a USB audio adapter and powered speaker, the LD2410B sensor and a weatherproof box | The whole show: sound, fog, presence-triggered scares. This is roughly the reference rig | **about $650 to $1,000** |
| 🦇 **Neighborhood Legend** | 6 to 10 outdoor PARs, low-lying or name-brand fog, a PA speaker, an isolated DMX adapter, wired Ethernet, a spare SD card with a backup image | A bigger, more robust yard | **$1,500 and up** |

Item-by-item prices are in the [README](../README.md#what-it-costs). They're rough US street prices from October 2026, and a memory shortage has pushed Raspberry Pi and SD card prices up sharply this year, so check before you buy.

> 🦇 **Going big?** Add each light under System → [Your rig](using-it.md#your-rig); the channel block grows to fit on its own, up to 512 channels (one universe). Also raise FPP's own DMX output channel count to reach your last device; the Setup check tells you when it falls short. Only 64 channels have been tested on hardware so far ([limits](limits-and-unknowns.md#recommended-limits)).

## The shopping list

### Required

| Item | Why | Notes |
|---|---|---|
| **Raspberry Pi** | Runs FPP and Haunt Director | ✅ Pi 5 (8 GB). 🤞 Pi 5 (4 GB): the whole stack, desktop included, uses about 1 GB on the reference rig. 🤞 Pi 4. See [unknowns](limits-and-unknowns.md#unknowns) for others |
| **Official Pi power supply** | Stops brown-outs and odd USB behavior | The Pi 5 wants the 27 W USB-C supply. The System tab warns you if it sees under-voltage |
| **microSD card** (32 GB+) | OS, FPP, your audio | ✅ 128 GB SanDisk. Audio is the only thing that takes real space. Buy a name brand, and once the rig works, save an image of the card: the reference rig's first card started stalling under load ([what that looks like](troubleshooting.md#the-pi-takes-minutes-to-start-or-doesnt-come-back-after-a-restart)) |
| **Falcon Player (FPP)** | Talks to the DMX hardware | ✅ FPP 9.5. See [Install](install.md#1-install-fpp) |
| **USB to DMX adapter** | Turns USB into a DMX signal | ✅ ENTTEC Open DMX USB (FTDI FT232R) as FPP `DMX-Open`. 🤞 anything FPP supports, like `DMX-Pro` adapters |
| **DMX fixtures** | The lights! | ✅ HOLDLAMP IP65 RGBW PAR, 8 channel mode. 🤞 starter profiles for common 3, 4 and 6 channel layouts. Others need a [profile](fixture-profiles.md). Outdoors, get IP65 (weatherproof) fixtures; indoor ones belong under cover |
| **DMX cables + a terminator** | Daisy-chain the fixtures | 3-pin XLR is typical for these lights. Put a 120 Ω terminator in the last fixture's DMX OUT |
| **A phone or laptop** | Running the app | Any modern browser on the same network |

### Optional, but they're the fun part

| Item | Why | Notes |
|---|---|---|
| **DMX fog machine** | Rolling fog, timed bursts | ✅ a 1-channel DMX fogger. Any fogger with a DMX "output" channel. Ones with built-in LEDs use several channels and need a [profile](fixture-profiles.md) |
| **USB audio adapter** | Sound out of the Pi | ✅ Griffin iMic. 🤞 most class-compliant USB audio adapters. The Pi 5 has **no** headphone jack |
| **Powered speaker** | So the neighbors can hear the zombies (or the sleigh bells) | Anything with a wired line, RCA or AUX input. Plenty of newer Bluetooth speakers have dropped the AUX jack, so check. Outdoor rated if it lives outside |
| **Presence sensor** | Fires scares when someone walks up | ✅ HLK-LD2410B 24 GHz mmWave (digital OUT). 🤞 a PIR or any active-high 3.3 V output |
| **Cat5/6 cable + RJ45 screw-terminal adapters** | Running the sensor a long way from the Pi | ✅ what the reference rig uses to reach the porch |
| **Static RGB spotlights** | Fill light | Not controllable (no DMX). The app can still show them in the simulator so your preview matches the yard |
| **Big props** | Atmosphere | A giant yard skeleton looks fantastic lit from below. So does an inflatable snowman. Props with their own electronics just stay independent |
| **Tailscale** (or another VPN) | Checking on the show from anywhere | ✅ free tier. See [Security](../SECURITY.md) |

## The reference rig (exactly what this was tested with)

| Part | Details |
|---|---|
| Computer | Raspberry Pi 5 Model B Rev 1.1, 8 GB RAM, 128 GB SanDisk microSD with SD command queueing off (`dtparam=sd_cqe=off`) |
| OS | Raspberry Pi OS with desktop (Debian 12 "Bookworm"), 64-bit, kernel 6.12 |
| FPP | 9.5, installed on top of Raspberry Pi OS with FPP's install script |
| Python | 3.11.2, packages as pinned in `backend/requirements.txt` |
| DMX adapter | ENTTEC Open DMX USB (FTDI FT232R), FPP output `DMX-Open` on `/dev/ttyUSB0` |
| Lights | 3 × HOLDLAMP IP65 RGBW PAR in 8 channel mode at DMX 1, 17 and 33 |
| Fog | 1-channel DMX fogger at DMX 49 |
| Audio | Griffin iMic USB audio adapter into a powered outdoor speaker |
| Sensor | HLK-LD2410B, OUT on GPIO 4 (BCM), powered from the Pi's 5 V pin, run out to the porch over Cat5. It lives in a pill bottle, cable and all, wrapped in self-fusing silicone tape; the radar sees straight through it |
| Network | Pi on Wi-Fi (about -62 dBm at the porch), Tailscale for remote access |
| Extras | 2 static solar RGB spotlights, one 12-foot yard skeleton |
| Browsers | Phone and desktop browsers on the same Wi-Fi, plus over Tailscale |

## Wiring it up

```mermaid
flowchart LR
  subgraph Pi[Raspberry Pi]
    FPP[Falcon Player]
    HD[Haunt Director]
  end
  Phone[📱 your phone] -- Wi-Fi --> HD
  HD -- light frames (DDP) --> FPP
  FPP -- USB --> DMX[USB to DMX adapter]
  DMX -- DMX --> L1[PAR 1 @ 1] --> L2[PAR 2 @ 17] --> L3[PAR 3 @ 33] --> F[Fogger @ 49] --> T((terminator))
  HD -- USB --> A[USB audio] --> S[🔊 powered speaker]
  Sensor[LD2410B sensor] -- GPIO --> HD
```

### DMX

1. Chain the adapter's output to fixture 1's **DMX IN**, then fixture 1's **OUT** to fixture 2's **IN**, and so on.
2. Put a **terminator** in the last device's OUT. It's a tiny plug and it fixes a surprising amount of flicker.
3. Set each fixture's **address** on its little display (usually MODE/UP/DOWN/ENTER). Leave gaps so you can change modes later without re-addressing everything:

| Device | Address | Reserved |
|---|---|---|
| Light 1 | 1 | 1 to 16 |
| Light 2 | 17 | 17 to 32 |
| Light 3 | 33 | 33 to 48 |
| Fogger | 49 | 49 to 64 |

4. Put every light in the **same channel mode** your profile describes (the reference HOLDLAMPs use 8 channel mode: dimmer, red, green, blue, white, strobe, function, speed).

> 💡 **Tip:** in FPP's DMX output settings, set the channel count to just past your last fixture (64 above) instead of 512. Each DMX frame gets about eight times shorter, so cheap adapters with no frame buffer of their own stay smoother. The System tab suggests this when it applies.

> ⚠️ The ENTTEC Open DMX USB and its clones are **not electrically isolated**. The Pi's ground goes out along the DMX cable into a damp yard. It worked fine on the reference rig, but keep connections dry and see [Safety](safety.md#outdoor-power-and-water). Isolated adapters cost more and remove the worry.

### Presence sensor (LD2410B)

| Sensor pin | Pi pin | Notes |
|---|---|---|
| VCC | 5 V (pin 2 or 4) | The module wants 5 V |
| GND | GND (pin 6) | |
| OUT | a free GPIO, e.g. GPIO 4 (pin 7) | 3.3 V logic, HIGH when someone is there. Set the pin number under **Show → Presence sensor** |
| TX / RX | not connected | Only used for configuring it from a computer |

- A small **series resistor (about 1 kΩ)** on OUT costs nothing and protects the Pi from a wiring slip.
- Long run? Use a spare Cat5/6 cable with **RJ45 screw-terminal adapters** at each end. Put power, ground and OUT on separate pairs, each paired with a ground.
- Seal the far end against rain. The app pulls the input toward "clear", so a connector that works loose reads as an empty yard rather than phantom scares, which also means you won't notice it unless you walk past and check. On the reference rig the whole sensor end, cap, cable and all, is wrapped in self-fusing silicone tape.
- Configure the sensor's range and sensitivity with the maker's Bluetooth phone app, then **turn its Bluetooth off** for the show. Radar sees *motion*, not people: swinging props, blowing fabric and fog plumes can all trigger it. Aim it along the path, not at the decorations. On the reference rig it faces the approach path and away from the yard props.
- On a Pi 5 some pins are claimed by other functions on certain OS versions. If your pin doesn't read, try another free GPIO (17, 22, 23, 24, 25, 27 are common choices).

### Audio

Plug the USB adapter into the Pi and its output into a powered speaker. Then, in the app, open **System**. If the Pi is defaulting to its HDMI port (it often does when no monitor is attached), the Sound check offers a one-click fix that points audio at your USB adapter.

Set the speaker's own volume once, sensibly loud, and ride the level from the app's **Volume** slider after that. A little less bass and a little more mid-range on the speaker helps voices cut through outdoors.

## Power and placement

- Use the official supply. Under-voltage on a Pi 5 shows up as random USB weirdness first (your DMX adapter and sound card are both USB).
- Give the Pi a ventilated, weatherproof box. The System tab shows temperature and warns about heat throttling.
- Wi-Fi at the porch matters. The reference rig runs fine around -62 dBm. A weak signal makes boots slower (see [Troubleshooting](troubleshooting.md#the-app-says-fpp-offline-after-a-reboot)). Wired Ethernet is even better.
- Give the Pi a **fixed address** on your router (a DHCP reservation) so the app is always at the same URL.
- Keep a **backup image** of the SD card once everything works. A fresh card plus that image turns a dead card into a quick fix instead of a lost evening.

## Beyond Halloween

Nothing needs rewiring for Christmas or any other holiday. It's the same lights wearing different colors:

- Set **House lights** (top of the Live tab) to the season's colors on a dusk schedule: red, green and warm white in December, red, white and blue for the Fourth, and so on.
- Swap the sound library: sleigh bells instead of rattling chains. Make sure you have the rights to any music you use.
- Switch the fog master off (**Show → Output → Fog output**) when fog doesn't suit the occasion.
- A **DMX snow machine** is the fogger's festive cousin. Nobody has tried one with Haunt Director yet. If you do, give it a profile with the `fog` role so the same burst caps apply, and [tell us how it went](https://github.com/phonoodlesramen/haunt-director/issues).
- December nights are colder than October ones, and cold nights are [untested](limits-and-unknowns.md#unknowns). Check the operating temperatures of your fixtures and fogger.
