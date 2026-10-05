# Changelog

## Unreleased

- Scenes: **⛶ Full screen** play view. The house, a whole-scene bar with a song show's sections, and a lane per light, fogger and sound sliding past a NOW line, so you can follow every trigger. Works in Sim and Live
- Scenes: **🖥 Sim / 🏠 Live** switch. Sim plays a scene on the stage and this device's speaker only, never the yard; while the show is armed, Play is always Sim
- Generate: **🎸 Song show** replaces beat sync. Drums, bass and rhythm guitar each drive a light, sections come from timed lyrics (LRCLIB) or the music, and the AI picks colors from what the words mean. Only timings and colors are saved, never lyrics. Extend works on song shows too
- Output: **Light timing** (-300 to +300 ms) and a **⏱ Sync test** to line the lights up with the sound
- Playback: long, busy scenes take about half the work per frame (only events that have started are replayed)
- Audio: at startup, the sound cache drops its converted copies of clips that are gone
- Generate: scenes up to **5 minutes**. Long scenes are planned as an arc and written in parts; the amount of action grows with the length; the YouTube clip matches the scene length; beat sync goes to 5 minutes too
- Scenes: **✨ Extend** adds 30, 60 or 90 seconds to an existing scene, continuing its look (beat-locked for beat-sync scenes) and fetching a longer clip of the same song from the same spot. Undo takes it back
- Safety: generated fog bursts always have 10 s of clear air between them and are spread across the scene; generated strobes are capped at 8 s each
- Fix: generating with a YouTube link no longer deletes a full import of the same video that other scenes use
- Fix: the scene editor's header no longer runs off narrow phones
- Clearer messages when the AI service times out, rate-limits or refuses the key
- Show: **Sensor fires scares** switch. Stops the sensor firing without forgetting its pin; it keeps reading, and the button still fires
- Show: **Ambient between scares** switch for sensor-only shows that sit dark and quiet between scares
- Show: **Hold before firing** (0 to 5 s) so a scare only fires if someone stays in range
- Fix: with no ambient scene to hand off to, a finished scare's sound no longer keeps looping and its last look no longer stays on the rig
- Fix: Show tab card headers and schedule rows lay out properly on phones
- System tab: a switched-off sensor shows as a yellow reminder

## 0.1.0 (October 2026)

The first public release. 🎃

- Phone-first web app: Live, Scenes and timeline editor, Show, Library, System
- Show mode: ambient rotation with audio crossfades, presence-triggered scares, cooldown, dusk schedules
- One output bus at 25 fps, with DDP into FPP and a 1-second dead-man switch
- Pi-side audio engine: music, voice and layers, master volume, automatic voice ducking
- Safety rails: fog master switch, capped self-expiring fog bursts, optional global strobe cap, profile bands that keep fixtures out of their auto modes, STOP EVERYTHING
- System tab: FPP and Pi health, a setup check with one-click fixes, restart and reboot buttons
- Optional AI scene drafting and beat sync (bring your own key)
- Data-driven fixture profiles, 23 example scenes, 5 original CC0 placeholder sounds
- Installer, systemd service, an exact-command sudoers rule, tests that need no hardware
