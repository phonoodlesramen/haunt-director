# 💡 Fixture profiles

Every DMX light speaks its own little dialect. Channel 1 might be the dimmer on one PAR and red on another, and "strobe off" might be 0 or anything from 0 to 10. Haunt Director never guesses. Each light model gets a **profile**: one small JSON file that says what each channel does.

Write one profile per *model and mode*, then point any number of devices at it (pick it under System → [Your rig](using-it.md#your-rig)).

## What's included

The app ships a small library in `backend/data-example/profiles/`. It's read straight from there, so updating Haunt Director brings new and fixed profiles to an existing install.

| Profile | Channels | Status |
|---|---|---|
| `holdlamp-rgbw-8ch` | dimmer, R, G, B, W, strobe, function, speed | ✅ tested on the reference rig |
| `generic-fog-1ch` | fog output | ✅ tested on the reference rig |
| `generic-rgb-3ch` | R, G, B | 🤞 starter |
| `generic-rgbw-4ch` | R, G, B, W | 🤞 starter |
| `generic-dimmer-rgbw-strobe-6ch` | dimmer, R, G, B, W, strobe (0 to 5 off, 6 to 255 slow to fast) | 🤞 starter |

**Starter profiles** are common channel layouts for cheap PARs, not tested on any particular light. Put your light in the matching DMX mode, check the order in its manual, and try a color and a strobe from a Live card before you trust it in a show. A light with no dimmer or strobe channel still works: brightness is mixed into the color, and strobes show as steady light.

**Your own profiles** go in `backend/data/profiles/` (git-ignored, so updates leave them alone). One with the same `id` as a library profile replaces it. Restart the app after adding one.

## The reference PAR

```json
{
  "schema_version": 1,
  "id": "holdlamp-rgbw-8ch",
  "vendor": "HOLDLAMP",
  "model": "IP65 RGBW PAR",
  "mode_name": "8CH",
  "channel_count": 8,
  "has_master_dimmer": true,
  "channels": [
    { "offset": 0, "role": "dimmer",   "name": "Master Dimmer", "default": 0 },
    { "offset": 1, "role": "red",      "name": "Red",   "default": 0 },
    { "offset": 2, "role": "green",    "name": "Green", "default": 0 },
    { "offset": 3, "role": "blue",     "name": "Blue",  "default": 0 },
    { "offset": 4, "role": "white",    "name": "White", "default": 0 },
    { "offset": 5, "role": "strobe",   "name": "Strobe", "default": 0,
      "off_band":  { "lo": 0, "hi": 0 },
      "rate_band": { "points": [[0, 0], [1.25, 130], [2.4, 200], [8, 255]] } },
    { "offset": 6, "role": "function", "name": "Function", "default": 0 },
    { "offset": 7, "role": "speed",    "name": "Speed", "default": 0 }
  ]
}
```

And a fogger:

```json
{
  "schema_version": 1,
  "id": "generic-fog-1ch",
  "vendor": "Generic",
  "model": "DMX Fog",
  "mode_name": "1CH",
  "channel_count": 1,
  "has_master_dimmer": false,
  "channels": [
    { "offset": 0, "role": "fog", "name": "Fog Output", "default": 0,
      "off_band": { "lo": 0, "hi": 5 } }
  ]
}
```

## The fields

| Field | Meaning |
|---|---|
| `id` | What devices refer to in `profile_id`. Use something like `vendor-model-8ch` |
| `vendor`, `model`, `mode_name` | How it's named in the profile list (`HOLDLAMP IP65 RGBW PAR · 8CH`) |
| `tested` | `true` once it's been checked on a real fixture. Untested ones are marked "starter" in the app |
| `notes` | Shown under the profile when you pick it: the mode to set, anything odd |
| `channel_count` | How many channels this mode uses. Must match the mode set on the fixture's display |
| `has_master_dimmer` | `true` if a dimmer channel controls overall intensity. If `false`, brightness is baked into the color values instead |
| `channels[].offset` | 0-based position from the device's DMX address (address 17, offset 2 = channel 19) |
| `channels[].role` | What the channel does (see below) |
| `channels[].default` | The value sent when nothing else asks for one |
| `off_band` | The range that means "off". For strobe, Haunt Director sends its midpoint when the strobe is off. Fog off is always sent as 0 |
| `rate_band` | How strobe rates map to values (see below) |
| `bands` + `required_band` | Named ranges on banded channels, and one the channel must *always* stay in (see below) |

**Roles:** `dimmer`, `red`, `green`, `blue`, `white`, `amber`, `uv`, `strobe`, `function`, `macro`, `speed`, `mode`, `fog`, `fan`, `unused`.

Colors are sent as RGB today. Whenever a color is set, white is held at 0. Amber and UV stay at their defaults.

## Strobe rates

Fixtures strobe from their **own** strobe channel. Haunt Director never fakes a strobe by flashing frames on and off, because frame timing over USB DMX is too jittery for that to look right. There are two ways to describe the mapping:

**Measured points (best).** Pairs of `[flashes_per_second, dmx_value]` that you measured on the real fixture. Haunt Director interpolates between them. Cheap fixtures are often very non-linear, which is why the reference PAR needs four points:

```json
"rate_band": { "points": [[0, 0], [1.25, 130], [2.4, 200], [8, 255]] }
```

**A simple range.** For a fixture that's roughly linear:

```json
"rate_band": { "lo": 11, "hi": 255, "min_hz": 1, "max_hz": 20 }
```

To measure: set the strobe channel to a value with FPP's channel tester or a Live card, film it on your phone, and count flashes over a few seconds. Do this for a handful of values.

## Keeping a fixture out of its auto modes

Many cheap fixtures have a "function" or "macro" channel where some ranges mean *listen to DMX* and others mean *run my own color-chase program and ignore you*. If that channel ever drifts into an auto range, your scene goes out the window, usually during the scare.

Describe the bands and mark the safe one as `required_band`. The resolver pins the channel to the middle of that band on **every** frame, no matter what:

```json
{ "offset": 6, "role": "function", "name": "Function", "default": 0,
  "bands": [
    { "lo": 0,   "hi": 50,  "label": "DMX control", "semantic": "dmx" },
    { "lo": 51,  "hi": 100, "label": "Jump",        "semantic": "jump" },
    { "lo": 101, "hi": 255, "label": "Auto / sound","semantic": "auto" }
  ],
  "required_band": "dmx" }
```

(The reference PAR behaves with its function channel at 0, so its profile just uses `default: 0`.)

## Finding your fixture's channel chart

1. Check the manual. If there isn't one, or it's wrong (it happens!), keep going.
2. Set the fixture's mode on its display (often shown as `d-8`, `CH08` or similar) and note the channel count.
3. Use FPP's **channel tester** or a scratch profile with every channel as `unused`, then raise one channel at a time and watch what happens.
4. Write down each channel's role, which values mean "off" for strobe, and any banded ranges.
5. Save the profile in `backend/data/profiles/`, restart the app, pick it for your light under System → Your rig, and test with a Live card (and **Flash it**).

## Share it! 🙏

If you write a profile for a fixture, please [open a pull request](../CONTRIBUTING.md#fixture-profiles). Name it `vendor-model-Nch.json`, and mention the exact product and mode in the PR. Every profile saves the next person an evening with a flashlight and a notepad.
