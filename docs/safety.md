# 🦺 Safety

The goal is screams of delight, not a trip to urgent care. A few things are worth taking seriously, and Haunt Director has rails for several of them. None of this is professional advice. Check your local rules, and your fog machine's and fixtures' manuals.

## Strobes and photosensitivity

Flashing light can trigger seizures in people with photosensitive epilepsy, and on Halloween you don't know who's walking up your path.

- The UK's Epilepsy Society says flicker between about **3 and 30 flashes per second** can be a trigger, with **16 to 25** the most likely. About **3%** of people with epilepsy are photosensitive. ([Epilepsy Society](https://epilepsysociety.org.uk/node/3176))
- For public venues, the UK Health and Safety Executive recommends strobes flash at **4 per second or less** (as cited by the [Epilepsy Society](https://epilepsysociety.org.uk/node/3176)).

**What Haunt Director gives you:** `HD_STROBE_MAX_HZ` caps every strobe, from scenes, Live cards and AI drafts alike. The `.env.example` that new installs start from sets it to **4**. You can raise it or set `0` for no cap, but please think about who's watching first. Some example scenes are designed faster than 4 per second. With the cap on, they still strobe, just more slowly.

**Also consider:** a small sign at the start of the path ("Flashing lights and fog ahead!"), keeping strobe bursts short, and not aiming strobes straight at the path where people look into them.

## Fog

- **Bursts are capped and self-expiring.** No single burst can run longer than `HD_FOG_MAX_SECONDS` (8 by default). Bursts switch themselves off even if your phone drops off Wi-Fi. If the app stops, FPP blanks the output within a second.
- **One master switch.** **Show → Output → Fog output: off** holds the fogger at zero regardless of any scene or button. Rehearse with it off.
- **Visibility.** Fog over steps and uneven ground hides hazards. Keep the path itself clear, or light it.
- **Slippery residue.** Fog fluid can leave surfaces slick, especially on smooth porch floors and steps.
- **Hot nozzle.** Fog machines get very hot. Keep them out of reach of curious hands, and away from leaves, costumes and fabric.
- **Smoke alarms.** Fog that drifts indoors or under a covered porch can set detectors off.
- **Breathing.** Some people, especially those with asthma, are bothered by fog. Don't aim it at faces. Use the fluid your machine's maker recommends.
- **Presence sensors see fog.** Radar sensors can be triggered by a moving fog plume. Keep the sensor away from the fogger's output, or you'll get a scare-fog-scare loop.

## Outdoor power and water

- Plug everything into **GFCI (US) / RCD (UK and EU) protected** outlets, using outdoor-rated extension cords.
- Keep connections **off the ground** and give cables a **drip loop** (a dip below the connection, so water runs off before reaching the plug). Weatherproof boxes for the Pi and power bricks are cheap.
- **The ENTTEC Open DMX USB and its clones aren't isolated.** Your Pi's ground runs along the DMX cable into the yard. It's usually fine, but a wet connector or ground loop can damage the Pi. Keep DMX connectors dry and terminated, or use an isolated adapter.
- Treat "IP65" fixtures as splash-proof, not submersible. Don't leave connectors lying in puddles.

## Trip hazards

Run cables along edges, tape or cover them where people walk, and never across the path. The fog will hide them from you, too.

## Sound

- Be a good neighbor. Use the **schedule** to keep the show to sensible hours, and keep the volume reasonable after the main trick-or-treat window.
- Scare stingers are loud by design. Set the speaker level with the **Test** button *before* the first visitor, not during.

## Know your audience 🎈

Not every visitor wants the full Tempest. Little kids in the early evening may enjoy spooky-fun more than terrifying. Scenes with no role are manual-only, so one easy trick is to leave the most intense scares untagged until the little ones have gone home, then tag them *scare* for the late crowd. And if a scare goes wrong, the **■ STOP EVERYTHING** button is on every screen.
