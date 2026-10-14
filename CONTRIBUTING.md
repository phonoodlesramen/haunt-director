# 🤝 Contributing

Thanks for wanting to help make more yards scarier (or merrier)! Bug reports, fixture profiles, docs fixes and code are all welcome. Be kind. Everyone here is doing this for fun.

## Ways to help

- 💡 **Fixture profiles.** The single most useful contribution. See [below](#fixture-profiles).
- 🧪 **Try an unknown.** Run it on a Pi 4, FPP 10, the FPP SD image, a different DMX adapter or sensor, and [report how it went](https://github.com/phonoodlesramen/haunt-director/issues). Successes count as much as failures.
- 🐛 **Bugs.** Include your Pi model, FPP version, DMX adapter, what the System tab shows, and the relevant bit of `journalctl -u haunt-director`.
- 🌍 **Docs.** If something confused you, it will confuse the next person too.

## Development setup

You don't need a Pi or any lights to work on most of it.

```bash
git clone https://github.com/phonoodlesramen/haunt-director.git
cd haunt-director/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests swap the hardware path for a recording transport, so they run anywhere (including CI). To run the app on a laptop with no FPP, start it and use the UI. FPP will show as offline and the System tab's checks will read "unknown", but scenes, the editor, the stage simulator and the library all work:

```bash
HD_AUDIO_ENABLED=false .venv/bin/uvicorn haunt_director.api.app:app --reload --port 8000
```

[How it works](docs/how-it-works.md) has a map of the code.

## Fixture profiles

1. Work out your fixture's channels ([guide](docs/fixture-profiles.md#finding-your-fixtures-channel-chart)).
2. Add `backend/data/profiles/<vendor>-<model>-<N>ch.json`.
3. Test it on the real light: color, brightness, strobe at a few rates, and strobe off.
4. Open a pull request with the exact product name and link, the mode you used, and anything odd (non-linear strobe, auto-mode bands).

## Pull requests

- Keep them focused. One fix or feature per PR is easiest to review.
- Add or update tests for behavior changes. Anything touching **fog, strobe, the output bus or STOP** needs a test, no exceptions. Those are the safety-critical paths.
- Run `pytest` before pushing.
- Match the existing style: plain Python with type hints where they help, and comments that explain *why*.
- UI text should be short and friendly. People read it on a phone, in the dark, while holding a bowl of candy.

## Ideas we'd love help with

Optional login for remote access · moving-head support (pan, tilt, gobos) · a devices editor in the UI · more sensors and per-zone triggers · Home Assistant integration · per-scene or time-of-night scene pools · translations · a proper isolated-DMX recommendation list.

By contributing you agree your contributions are licensed under the project's [MIT license](LICENSE).
