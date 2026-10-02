# 🩹 Troubleshooting

Every entry here really happened on the reference rig on the way to Halloween. **Start with the System tab.** It catches most of these and fixes several with one tap.

## The app says "FPP offline" after a reboot

**What's happening:** on a slow boot (weak Wi-Fi at the far end of the yard is the classic cause), FPP's web server (Apache) and its PHP service can hit systemd's 90-second start limit and give up. FPP itself is running fine, but its web API is down, and it **stays down** until something restarts it. Waiting longer won't help.

**Fix:** System tab → **FPP web UI and API → Restart FPP web services**. Then fix it for good with **Web services retry after a slow boot → Add auto-retry**, which makes them retry every 5 seconds instead of giving up. Neither affects the lights.

Better Wi-Fi at the Pi (or a cable) shortens boots in general.

## "Pi audio isn't ready"

**What's happening:** the Pi's default sound output is its HDMI port. With no monitor attached, HDMI refuses to open ("error 524"), so the app never reaches your USB adapter. It tends to show up the first time the rig runs somewhere without a monitor.

**Fix:** System tab → **Sound output → Use (your adapter)**. It points ALSA's default device at the USB adapter by name (in `~/.asoundrc`, surviving reboots), then restarts the app.

## I can't find the Pi anymore

Moving the Pi (say, from the kitchen table out to the yard) can earn it a new address from your router. Find it in the router's app or client list, then give it a **DHCP reservation** so it keeps that address. A VPN like Tailscale gives it a stable address too.

## Lights show the wrong color after a scene change, or strobes and short fades get skipped

That was the old way of sending light data, with individual channel updates through FPP's HTTP API stacking up inside FPP. Make sure `backend/.env` has `HD_OUTPUT_TRANSPORT=ddp` (the default for new installs), restart the app, and check the System tab's **FPP accepts Haunt Director's light data (DDP)** row.

## A light ignores everything, or runs its own color show

- **Wrong mode or address.** Check the fixture's display matches its profile's channel count and the `dmx_address` in `devices.json`.
- **It's in an auto program.** Its function or macro channel is in an "auto" range. Add a `required_band` to the profile ([how](fixture-profiles.md#keeping-a-fixture-out-of-its-auto-modes)).
- **Flicker or random flashes** usually mean a missing **terminator** on the last fixture, or a long, cheap cable.
- **One light responds to another's controls?** Overlapping addresses. Leave 16-channel gaps.

## The strobe keeps going after a scene ends

Some fixtures need a few frames with the strobe channel at "off" before they actually stop. Set `HD_STROBE_CLEAR_FRAMES=3` in `backend/.env` and restart.

## Scares fire when nobody's there

24 GHz radar sees **motion**, not people: swinging props, flapping fabric, branches and fog plumes all count, and it can see through thin walls and windows. Re-aim it along the approach path and away from the decorations, shorten its range and sensitivity in the vendor's phone app, and keep it away from the fogger. A longer **cooldown** under Show helps too. (If it never fires, check you're testing in front of the sensor's face, not behind it.)

## Voices are hard to understand

Music and ambience are covering the frequencies speech lives in. Turn up **Voice ducking** (Show → Output; try 12 dB). On the speaker, a little less bass and a little more mid-range helps outdoors. In the Library, make sure the clip is filed as **Voice**, or it won't trigger ducking.

## YouTube import fails

```bash
cd haunt-director/backend
.venv/bin/pip install -U yt-dlp
sudo systemctl restart haunt-director
```

## "No LLM API key configured"

Generate and Beat Sync need your own AI key. Set `HD_LLM_PROVIDER`, `HD_LLM_API_KEY` and `HD_LLM_MODEL` in `backend/.env` and restart. Everything else works without one.

## Power warnings on the System tab

"Under-voltage" means the Pi isn't getting enough power, and it often shows up first as USB devices (your DMX adapter and sound card) misbehaving. Use the official Pi 5 27 W supply and avoid long or thin USB-C cables.

## Still stuck?

Look at the app's log:

```bash
journalctl -u haunt-director -n 100 --no-pager
```

…and [open an issue](https://github.com/phonoodlesramen/haunt-director/issues). Include your Pi model, FPP version, DMX adapter and what the System tab shows. Please leave out anything private, like your `.env`.
