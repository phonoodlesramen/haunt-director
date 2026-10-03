# Changelog

## Unreleased

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
