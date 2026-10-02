"""Runtime configuration.

Defaults target the app running ON the Pi (FPP at localhost). For dev against
a remote Pi, set HD_FPP_BASE_URL=http://<your-pi>.local  (LAN may require the FPP
UI password; localhost does not). Every setting can be set as HD_<NAME> in backend/.env.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HD_", env_file=".env", extra="ignore")

    # --- Falcon Player ---
    fpp_base_url: str = "http://127.0.0.1"
    fpp_timeout_s: float = 6.0

    # --- DMX universe / overlay model ---
    # One Pixel Overlay Model spans the whole DMX control band. Manual control
    # writes into it; fppd forces those channels onto the output every frame.
    overlay_model_name: str = "haunt"
    dmx_start_channel: int = 1        # 1-based, matches FPP
    dmx_channel_count: int = 64       # 64, not 512: see docs/hardware.md (DMX addressing)

    # --- Channel output for dev/testing ---
    # "e131" = sACN multicast (works with no hardware, validated 23 Sep).
    # "none" = assume an output is already configured (e.g. real USB DMX).
    # Default "none": the E1.31 multicast test output from Sept 23 was still being
    # re-provisioned on every start and multicasting over Wi-Fi 20x/s.
    output_kind: str = "none"
    e131_universe: int = 1

    # --- Light output path (output.py: one writer, one frame per tick) ---
    # "rest" = FPP overlay ranges, one channel per range (no FPP config needed)
    # "ddp"  = FPP DDP input on UDP 4048 (needs Channel Inputs enabled in FPP)
    output_transport: str = "rest"
    output_fps: int = 25
    ddp_host: str = "127.0.0.1"
    ddp_port: int = 4048
    # Legacy strobe-latch workaround (hold a fixture dark N frames after a
    # strobe). It was compensating for FPP's stale-range bug, and itself created
    # the stale "1-4 = 0" range that killed Skelly. 0 = off. Re-enable only if a
    # rig test on the fixed output path shows a real fixture latch.
    strobe_clear_frames: int = 0

    # --- Safety ---
    fog_max_seconds: float = 8.0      # hard ceiling on any single fog burst

    # --- Presence sensor (LD2410 OUT pin -> Pi GPIO) ---
    presence_gpio_pin: int | None = None       # BCM pin the sensor OUT is wired to
    presence_gpio_active_high: bool = True      # LD2410 OUT is HIGH on presence

    # --- Audio (Pi-side playback to a USB adapter -> powered speaker) ---
    audio_enabled: bool = True
    test_sound: str | None = None      # HD_TEST_SOUND: file in web/audio the Volume "Test" button plays
    audio_device: str | None = None   # SDL device name; None = auto-detect USB, else default
    audio_dir: str = "web/audio"      # source audio (served files), relative to backend/

    # --- LLM scene generation (authoring-time only; keys live in backend/.env) ---
    llm_provider: str = "anthropic"   # HD_LLM_PROVIDER: anthropic | openai
    llm_api_key: str | None = None    # HD_LLM_API_KEY
    llm_model: str = "claude-sonnet-5"  # HD_LLM_MODEL

    # --- safety ---
    strobe_max_hz: float = 0.0             # HD_STROBE_MAX_HZ: cap every strobe (scenes + Live). 0 = no cap.
                                           # Public-facing shows: 4 or less (see docs/safety.md).

    # --- this app's own service + web access ---
    service_name: str = "haunt-director"   # HD_SERVICE_NAME: systemd unit the Restart button bounces
    cors_origins: str = ""                 # HD_CORS_ORIGINS: comma list of extra origins allowed to call
                                           # the API from another site. Empty = same-origin only (safest).

    @property
    def dmx_end_channel(self) -> int:
        return self.dmx_start_channel + self.dmx_channel_count - 1


settings = Settings()
