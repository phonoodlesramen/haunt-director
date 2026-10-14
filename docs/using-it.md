# 🗺️ The tour

Five tabs along the bottom (along the top on a laptop), a status strip across the header, and one very large red button. Here's what everything does.

## The header

- **The signal strip** shows a pill for the sensor, every light and the fogger. Each light's pill glows in the color that fixture is *actually* being sent right now, whether it comes from a scene, a Live card or nothing at all. The sensor pill says CLEAR or PRESENT, and the fog pill lights up whenever fog is being sent.
- **The FPP pill** (top right) is green when FPP is answering. Tap it to jump to the System tab.
- **■ STOP EVERYTHING** is always there. It ends the scene, drops every manual override, cuts sound and zeroes the fogger, all at once.

## 💡 Live

Hands-on control of each fixture, for testing and for "make the porch purple right now" moments.

### House lights

The card at the top of Live lights the house on nights there's no show. It's **lights only**: it never touches the fogger and never plays a sound.

- **Off / Schedule / On.** On lights the house until you switch it off. Schedule follows the card's own windows, separate from the show's schedule.
- **Colors.** Give each light one to four colors. With one it holds steady; with more it glides from one to the next, slightly out of step with the other lights, so the house always looks a little different. **Each color lasts about** sets the pace, from a minute to an hour. **Brightness** dims the whole thing. The dot beside each light shows what it's showing right now, and **Preview an hour** plays an hour of the drift in 30 seconds.
- **Any season.** Orange and purple in October, red, green and warm white in December, red, white and blue in July. The colors are yours; the schedule doesn't know what holiday it is.
- **Schedule windows** turn on at dusk (plus or minus an offset, using the location on the Show tab) or at a set time, turn off at a set time, and run on the days you pick. A window can run past midnight.
- **The show always comes first.** While the show is armed, or while any scene plays, house lights step aside; they fade back in when the yard is free again, if they're still due on. Live light cards still sit on top of them.
- **STOP EVERYTHING** turns them off until the next window starts (in On mode, until you press On again).
- Edits apply as you make them, so you can stand in the yard and pick colors.

- Each light card has a color swatch, brightness, a strobe rate and quick presets (*Blood, Ember, Toxic, Phantom, Ice, Bone*). Press **Apply** to send it.
- A light you set here **overrides** the running scene and wears a **Manual** badge. Tap **Release** to hand it back to the scene. Arming the show releases everything automatically.
- The **fog card** fires a burst of a chosen length. Every burst has a hard cap (8 seconds by default) and turns itself off when it's done, even if your phone loses Wi-Fi mid-burst.
- **Yard lights** are for fixtures you can't control (solar spots, battery uplights). Record what they're set to so the simulator matches the yard.

<img src="images/phone-live.jpg" alt="Live tab" width="300">

## 🎬 Scenes and the editor

The **Scenes** tab has the **stage**, a simulator that draws your lights and fog over a photo of your house, above the list of scenes. Press **▶ Play** to run a scene, or **↻ Loop** to keep it going.

The **🖥 Sim / 🏠 Live** switch above the stage (and next to Play in the editor) decides where Play runs:

- **🏠 Live** plays on the real lights, fogger and the Pi's speaker, with the stage following along.
- **🖥 Sim** plays only on the stage, with the sound coming from the phone or laptop you're holding. The yard isn't touched, so it's the way to try out a new scene before the night. The stage keeps its lights with that sound: it waits for the song to start (a beat or so on a slow connection), then follows it, and every light change shows at full strength on its beat.
- **While the show is armed, Play is always Sim** (the switch says ARMED), so you can work on scenes at your desk while the show runs outside. The app also refuses to play, seek or mute a scene on the rig while armed.

Each phone or laptop remembers its own setting. Switching takes effect at once: whatever was playing the other way stops.

**⛶ Full screen** (on each scene card, above the stage, and in the editor) opens a play view made for following along:

- The house fills the top of the screen.
- Under it, a bar of the whole scene shows a song show's sections (intro, verse, chorus...). Tap it to jump.
- Below that, a lane for each light, the fogger and the sound slides past a fixed **NOW** line, about 15 seconds at a time, so you can see each trigger coming. Each event flashes as it fires, and the dot by each light's name shows its color right now. Strobes and pulses show their individual flashes.
- ⏪ ⏩ jump 5 seconds (or use the arrow keys), and Space plays and pauses. Pause only works in Sim, since the rig has no pause.
- It follows the Sim / Live switch. In Live it plays on the yard and the view keeps time with it, so you can stand outside with your phone and watch. Opening it from a scene card starts the scene in Sim, but it never starts the yard by itself.
- **✕** or Esc closes it, and **■ STOP** is STOP EVERYTHING. Turn a phone sideways for a bigger house.

Press ✎ on a scene to open the **editor**:

<img src="images/editor.jpg" alt="The timeline editor" width="100%">

- **One lane per device.** Double-click a lane to drop an event where you clicked, or tap an event to edit it.
- **Event types:** `set_color`, `fade_color`, `set_brightness`, `fade_brightness`, `pulse`, `strobe`, `blackout`, and `fog` on the fogger lane. Fades have curves (linear, ease in, ease out, ease in-out).
- **Sound lanes:** a **music** bed, a **voice-over**, and any number of **layers**. Looping layers are ambient beds (wind, rain); one-shots are hits (thunder, screams). **+ Sound** and **+ Voice** add them at the playhead, and you can **trim** a clip to just the part you want.
- **🔊 on a lane** mutes it while you work, for lights and sound alike.
- **Scene settings:** name, blurb, length, the scene's **role** (*ambient* or *scare*, used by show mode) and how long its sound fades out when the next scene takes over.
- **Shortcuts:** `Del` removes, `Ctrl+D` duplicates (light events land right after the original, sounds at the playhead), `Ctrl+C`/`Ctrl+V` copy and paste, `←`/`→` nudge (hold Shift for 1 s steps), `Space` plays and stops, `Ctrl+Z` undoes.
- It **autosaves**. **Revert** takes you back to how the scene was when you opened it.

The example scenes are a good place to start: *Lightning Storm*, *Zombie Horde*, *Seance* (hundreds of tiny candle-flicker fades) and *The Tempest* (a strobe-heavy scare).

### ✨ Generate (optional, needs your own AI key)

Type something like *"a slow green graveyard mist that builds to a screaming strobe hit at the end"*, pick a length (10 seconds to 5 minutes) and a **flow** (flat, slow build, build to a strike, waves, cold open), and an AI model drafts the light and fog tracks. Paste a YouTube link and it'll clip a fitting section of the same length as the scene's music. If the song is shorter than the scene, the song loops.

Scenes over a minute are written in parts: the model plans a beginning, middle and end first, then writes each part knowing the whole arc, so a 4-minute scene develops instead of repeating one idea. The amount of action grows with the length. A long scene takes about a minute to write.

Turn on **🎸 Song show** with a link to a song, and the lights follow the band instead:

- **Listening.** It finds the beat and the downbeats, then what the drums, bass and rhythm guitar are doing on every 16th note. It splits the song by frequency band (no AI and no heavy downloads) and runs in a few seconds on the Pi.
- **Reading.** It looks up timed lyrics on [LRCLIB](https://lrclib.net) (free, community-made). Repeated lines become choruses, wordless stretches become the intro, solos and outro, and the AI picks a palette for each section from what the words mean, plus a few accent moments on key lines. Only the timings and colors are saved, never the words. No lyrics found? The sections come from the music instead.
- **Playing.** The first light plays the drums (kick punches, snare flashes), the second the bass (a throb on every 8th note), the third the rhythm guitar (it hits on the chord stabs and drops dark in the gaps). Section changes crash into the new colors, choruses get a burst of fog, solos chase around the yard, and the song ends on a final hit. Change anything afterwards in the timeline.
- It starts on the song's first note. Leave **Length** at 5:00 for the whole song. Describing the scene is optional (it steers the colors), and if the video title is odd, type *Artist - Title* in the Song box.

Song shows work best on rock and pop with a steady drum beat in 4/4. They assume the classic backbeat (kick on 1 and 3, snare on 2 and 4), so a half-time or swung song will look a bit off.

**✨ Extend** (in a scene's settings) adds 30, 60 or 90 seconds to a scene you already have, up to 5 minutes. The part you already have isn't changed, with one exception: a song show's ending (the strobe run-up, the last hit and the fade to dark) is lifted so it doesn't land mid-song. Anything you added there by hand stays, and a lane you cleared stays clear. The new part picks up from where each light ends and keeps the scene's colors and mood, and you can type what should happen next (*"build to a big finish"*). If the music is a YouTube clip, it fetches a longer clip of the same song from the same spot, so everything already on the timeline still lines up. Beat-synced scenes keep going on the beat, and a song show reads the rest of the song on the same bar lines, so a returning chorus gets the chorus's colors. **Undo** takes an Extend back.

What the model sends back is always **sanitized**: event types and parameters are checked, fog bursts are capped in length and count with at least 10 seconds of clear air between them, strobes are capped at 8 seconds each, and strobe and pulse rates are clamped. Your global strobe cap still applies on top. Everything stays fully editable afterwards. Set `HD_LLM_PROVIDER`, `HD_LLM_API_KEY` and `HD_LLM_MODEL` in `backend/.env` to turn it on. It works with Anthropic or OpenAI keys, and you pay your provider per request.

## 🎃 Show (and Output)

<img src="images/phone-show.jpg" alt="Show tab" width="300">

### Output

| Control | What it does |
|---|---|
| **Fog output** | The master fog switch. Off holds the fogger at zero no matter what any scene, card or AI asks for. Cues still show on the signal strip, so you can rehearse fog-free |
| **💨 Test** | A 2-second fog burst through the same capped, self-expiring path as the Live card. Refused while fog output is off |
| **Sound** | Silences all music, voice and effects |
| **Volume** | The master level for everything the Pi plays. It's a perceptual curve, so 50% sounds about half as loud. Set the speaker once, then ride it here |
| **🔊 Test** | A short clip at the current volume |
| **Voice ducking** | While a voice plays, music and looping beds dip by this many dB (default 9), then ease back. One-shot hits stay full so scares keep their punch. 0 turns it off |
| **Light timing** | Lines the lights up with the sound for every scene, from 300 ms early to 300 ms late. Sound out of the Pi and light through FPP don't arrive at exactly the same moment, and on a song show you can see it. Set early, a scene holds its sound back instead of skipping its first moments, and either way the lights play the scene to its end. It's for the yard only: Sim keeps itself in time with the sound on your device |
| **⏱ Sync test** | 12 seconds of clicks through the speaker while a light flashes on each one (the first one not set by hand on the Live tab). Stand where visitors will, then slide Light timing until flash and click land together (it changes live) |

### Show mode

**Arm the show** and Haunt Director takes over:

1. It rotates through your **ambient** scenes, crossfading the sound between them.
2. When the **presence sensor** goes HIGH, it picks a random **scare** scene and fires it immediately. Scare audio is loaded into memory when the app starts, so there's no lag. A scene you newly tag as a scare loads on its first trigger, which takes a moment longer.
3. After the scare, it returns to ambient. A trigger that arrives during the **cooldown** waits until the cooldown ends, so one lingering visitor gets at most one scare per cooldown instead of six in a row.

Two switches under the Arm button change how the night runs. Both are saved, so they survive a restart:

- **Sensor fires scares.** Turn it off and the sensor stops firing scares but keeps reading, so the top strip still shows PRESENT and CLEAR (its pill says OFF). Handy while you're working in the yard with the show running, or for a lights-only night. **⚡ Trigger a scare now** still works. The System tab shows a yellow reminder while it's off.
- **Ambient between scares.** Turn it off for a **sensor-only show**: the yard sits dark and quiet, a visitor sets off a scare, and when the scare ends its sound fades out and the yard goes dark again. Arming in this mode starts from a dark, quiet rig too. Lights you set by hand on the Live card after arming are left alone.

Also here:

- **⚡ Trigger a scare now** fires one by hand.
- **Schedule** auto-arms during windows you define. The defaults are a nightly window starting at dusk and a long one for Halloween night. "Dusk" is computed from today's sunset at your **latitude and longitude**, so set those.
- **Presence sensor** sets the GPIO pin and which level means "present". **Remove pin** forgets the pin entirely (to keep it and just stop it firing, use the switch above). **Hold before firing** makes someone stay in range for up to 5 seconds before a scare fires, which filters out a quick pass, a passing car's reflection or a flicker. It's off (0) by default, and 1 to 2 seconds suits a walkway. Range and sensitivity still live in the sensor's own phone app.
- **Scene roles** shows which scenes are in the ambient and scare pools.

## 📚 Library

Every sound you can use in a scene:

- **Upload** a file (wav, mp3, ogg, m4a, flac), or **import from YouTube** with a link (only audio you have the rights to use, please).
- Each clip is filed as **Music**, **Ambience & SFX** or **Voice**. The category matters: clips filed as **Voice** are what trigger voice ducking.
- **Rename, recategorize, preview** or **delete**. Delete warns you if a scene still uses the clip. (Trimming lives in the scene editor.)

The repo ships with five placeholder sounds, synthesized from scratch and dedicated to the public domain (CC0): a dread drone, a wind bed, a storm bed, a thunder crack and a scare stinger. They're there so the example scenes make noise on day one. Swap in better ones!

## 🩺 System

<img src="images/phone-system.jpg" alt="System tab" width="300">

**FPP health** shows live tiles for FPP itself, FPP's web UI, the lights output (frames per second and errors), sound, the sensor, the Pi (temperature, power problems, load), the network (address and Wi-Fi signal) and uptime.

**Setup check** runs about a dozen checks on the things Haunt Director depends on. Most failures come with a one-click **Fix**, and the rest come with a hint. The full list is in [Install, step 4](install.md#4-run-the-setup-check). Fixes that would restart FPP or the app are refused until you disarm the show and stop any playing scene.

**Maintenance** has four buttons, and each one asks twice:

| Button | Effect on the show |
|---|---|
| **↻ Restart FPP web** | None. Restarts Apache and PHP, which is the fix when FPP's UI or API is stuck. DDP light data goes straight to FPP, so the lights keep running |
| **↻ Restart FPP** | Lights drop out for a few seconds, then resume |
| **↻ Restart app** | Lights go dark for a few seconds (the 1-second dead-man switch), then resume. The page reconnects by itself |
| **⏻ Reboot Pi** | Everything goes dark for 1 to 3 minutes. The page waits and reconnects |

**Open FPP's own settings ↗** links to FPP's full web UI for everything this panel doesn't try to do.
