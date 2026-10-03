# 🛠️ Install

From a blank SD card to a haunted yard. Budget about an hour the first time, most of it waiting for downloads.

1. [Install FPP](#1-install-fpp)
2. [Point FPP at your DMX adapter](#2-point-fpp-at-your-dmx-adapter)
3. [Install Haunt Director](#3-install-haunt-director)
4. [Run the setup check](#4-run-the-setup-check)
5. [Tell it about your rig](#5-tell-it-about-your-rig)
6. [Test everything](#6-test-everything)
7. [Updating, backing up, uninstalling](#updating-backing-up-uninstalling)

---

## 1. Install FPP

Haunt Director runs **beside** Falcon Player on the same Pi. FPP drives the DMX hardware, and Haunt Director drives FPP.

You have two routes:

| Route | Status | When to pick it |
|---|---|---|
| **A. Raspberry Pi OS + FPP's install script** | ✅ what the reference rig runs (FPP 9.5) | You want the proven path for Haunt Director |
| **B. The official FPP SD image** | 🤞 untested with Haunt Director | You already run FPP this way. It's FPP's officially supported install, and it should work. Please report back! |

### Route A (tested)

1. Flash **Raspberry Pi OS (64-bit, Bookworm)** with Raspberry Pi Imager. In the Imager settings, set a username, your Wi-Fi and turn on SSH.
2. Boot it, SSH in, and run FPP's installer. It's FPP's own script, and its header has the full details:

   ```bash
   curl -o FPP_Install.sh https://raw.githubusercontent.com/FalconChristmas/fpp/master/SD/FPP_Install.sh
   chmod 700 FPP_Install.sh
   sudo ./FPP_Install.sh --branch v9.5      # the FPP version Haunt Director was tested with
   ```

   Read the warnings the script prints. It expects a **fresh system** and makes big changes to it (it creates an `fpp` user, installs a web server and more). The FPP project only officially supports its own SD images, so script installs are "at your own risk" from their side too.
3. Reboot. FPP's own web UI is now at `http://<your-pi>/` (port 80).

### Route B (FPP image)

Follow FPP's instructions for flashing their image, then make sure you can SSH in as a user with `sudo`. The rest of this guide is the same.

## 2. Point FPP at your DMX adapter

In FPP's web UI, go to **Input/Output Setup → Channel Outputs → Other**, and add an output:

| Setting | Value |
|---|---|
| Type | `DMX-Open` for ENTTEC Open DMX USB and clones, `DMX-Pro` for "Pro" style adapters |
| Port | your adapter, usually `ttyUSB0` |
| Start channel | `1` |
| Channel count | just past your last fixture, e.g. `64` (see the [tip](hardware.md#dmx)) |
| Enabled | ✅ |

Save, and restart FPP when it asks. That's the only FPP setting you have to do by hand. The setup check handles the rest.

## 3. Install Haunt Director

On the Pi, as your normal user (not root):

```bash
git clone https://github.com/phonoodlesramen/haunt-director.git
cd haunt-director
./deploy/install.sh
```

The installer is safe to re-run. It:

- installs `ffmpeg` and `python3-venv` if they're missing
- adds your user to the `audio`, `gpio` and `video` groups (sound card, sensor, power and heat readings)
- builds a Python environment in `backend/.venv` and installs the pinned packages
- copies `backend/.env.example` to `backend/.env` (your settings live there)
- if your user can't already `sudo` without a password, installs [`deploy/sudoers.d/haunt-director`](../deploy/sudoers.d/haunt-director). That's a short, commented list of the root actions the System tab's buttons need
- installs and starts the `haunt-director` service, so it comes back after every reboot

Options: `--dry-run` shows what it would do, `--no-sudoers` skips the sudo rule, `--no-service` skips the service.

When it finishes, open **`http://<your-pi>:8000`** on your phone. Add it to your phone's home screen for one-tap access.

## 4. Run the setup check

Open the **System** tab. The top half shows live health, and below it is the **Setup check**:

| Check | What a fix does |
|---|---|
| FPP is running | **Restart FPP** |
| FPP web UI and API | **Restart FPP web services** (Apache and PHP). The lights aren't affected |
| Web services retry after a slow boot | **Add auto-retry**: a tiny systemd override so FPP's web UI doesn't stay dead after a slow boot |
| DMX output | Shows what FPP is sending to, and whether the adapter is plugged in. Fix it in FPP's UI (step 2) |
| Haunt Director's channel block in FPP | **Create it**: the FPP overlay model the app uses for its channels (normally automatic). Any other overlay models you have are left alone |
| FPP accepts Haunt Director's light data (DDP) | **Turn it on**: enables FPP's network input so it hears the app's light frames |
| No leftover E1.31 broadcast | **Turn it off**: only flags an output an older Haunt Director created |
| Lights output | Frames per second and errors from the app to FPP |
| Sound output | **Use your USB adapter** if the Pi is stuck on HDMI audio |
| Presence sensor | Which pin, and whether it's reading |
| Clock, power and temperature, disk | Hints only. Power turns red if there's an under-voltage or heat problem right now (a Pi 5 wants its official 27 W supply) |

Fixes that restart FPP or the app are refused until you disarm the show and stop any playing scene. Keep tapping until you see **"All checks pass. Your rig is ready to haunt."** 🎃

## 5. Tell it about your rig

### Your fixtures

Your show lives in `backend/data/`, which the app creates on first start by copying the examples from `backend/data-example/`. It's git-ignored, so updates never overwrite it. Devices are in `backend/data/devices.json`. The example matches the reference rig: three PARs at 1, 17 and 33, a fogger at 49, and a couple of non-DMX extras. Edit the names and `dmx_address` values to match yours:

```json
{
  "id": "light_skelly",
  "name": "Skelly Light",
  "kind": "dmx_fixture",
  "profile_id": "holdlamp-rgbw-8ch",
  "dmx_address": 1,
  "enabled": true
}
```

| `kind` | Meaning |
|---|---|
| `dmx_fixture` | A DMX light. Needs `profile_id` and `dmx_address` |
| `dmx_fog` | A DMX fogger. Needs `profile_id` and `dmx_address` |
| `static_rgb` | A light you can't control (solar spot, battery uplight). Shown in the simulator only |
| `prop`, `audio_sink` | Markers for the simulator |

Different lights? Write a [fixture profile](fixture-profiles.md) in `backend/data/profiles/`. It's one small JSON file per light model.

Restart the app after editing: `sudo systemctl restart haunt-director` (or tap **Restart app** on the System tab).

### Your house

The simulator draws your lights over a picture of a house. Until you add your own, it uses the illustrated example. Save a **daytime** photo of your place, taken straight on, as `backend/web/img/house.jpg` (git-ignored). The stage darkens it into night on its own. Then on **Scenes**, tap **Edit layout** and drag each light to where it really sits and where its beam lands.

### Your show settings

On the **Show** tab:

- **Presence sensor:** the GPIO pin (BCM numbering), whether HIGH means "someone's there", and an optional **hold before firing** (someone has to stay in range this long).
- **Sensor fires scares** and **Ambient between scares:** switch the sensor off without losing its pin, or run a sensor-only show that stays dark and quiet between scares.
- **Cooldown:** how long after a scare before another can fire.
- **Audio crossfade:** how long sound overlaps between scenes.
- **Schedule:** auto-arm at dusk (with an offset) and on specific dates. Set your **latitude and longitude** so "dusk" means *your* dusk. The default is Salem, Massachusetts, which is spooky but probably not where you live.
- **Scene roles:** in the editor's scene settings, tag scenes as *ambient* (rotates while armed) or *scare* (fires on presence).

### Your settings file

`backend/.env` holds a few switches (all optional):

| Setting | Default | What it does |
|---|---|---|
| `HD_OUTPUT_TRANSPORT` | `ddp` (in `.env.example`) | How light frames reach FPP. `ddp` is the fast, safe path. `rest` is a fallback |
| `HD_STROBE_MAX_HZ` | `4` (in `.env.example`) | Caps every strobe's flash rate. See [Safety](safety.md#strobes-and-photosensitivity) |
| `HD_FOG_MAX_SECONDS` | `8` | Hard ceiling on any single fog burst |
| `HD_PRESENCE_GPIO_PIN` | none | Sensor pin, if you'd rather set it here than in the app |
| `HD_TEST_SOUND` | the bundled stinger | Which file in `backend/web/audio/` the Volume **Test** button plays |
| `HD_LLM_PROVIDER`, `HD_LLM_API_KEY`, `HD_LLM_MODEL` | off | Optional AI scene drafting with your own Anthropic or OpenAI key |
| `HD_CORS_ORIGINS` | empty | Only if you serve a different frontend from another site |

Every option is in `backend/haunt_director/config.py` with a comment.

## 6. Test everything

1. **Live:** pick a color on each light card and press **Apply**. The right fixture should change. A light you set by hand wears a **Manual** badge until you tap **Release**, which hands it back to the show.
2. **Show → Output:** press **🔊 Test** (a short clip at the current volume) and **💨 Test** (a 2-second fog burst).
3. **Scenes:** play *Lightning Storm* and *Zombie Horde*. Watch the top strip: each pill glows in its fixture's live color.
4. **Show mode:** arm the show and walk past the sensor. The pill in the top strip flips to PRESENT and a scare scene fires.
5. **STOP EVERYTHING:** it should cut lights, fog and sound instantly. Press it once just to know what it feels like.

🎉 You're haunting.

## Updating, backing up, uninstalling

**Update:**

```bash
cd haunt-director
git pull
./deploy/install.sh          # picks up new Python packages and service changes
```

Disarm the show first. Your scenes, devices and `.env` are kept.

**Back up** the things that are yours: `backend/data/` (scenes, devices, profiles, settings), `backend/web/audio/`, `backend/web/img/house.jpg` and `backend/.env`. All of them are git-ignored, so `git pull` leaves them alone.

**Uninstall:**

```bash
sudo systemctl disable --now haunt-director
sudo rm -f /etc/systemd/system/haunt-director.service /etc/sudoers.d/haunt-director
rm -rf haunt-director
```

FPP is untouched, apart from what the app and its setup check changed: the network input, the overlay model named `haunt`, and the auto-retry overrides in `/etc/systemd/system/apache2.service.d/` and `php*-fpm.service.d/`. Any FPP file it changed has a `.haunt-backup` copy next to it in `/home/fpp/media/config/`. Remove or restore those by hand if you want them gone.
