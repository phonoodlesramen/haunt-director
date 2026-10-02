"""Load fixture profiles and devices from the data/ directory (M1: read-only)."""
from __future__ import annotations

import json
from pathlib import Path

from .domain.devices import Device
from .domain.profiles import FixtureProfile
from .domain.scenes import Scene

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "data-example"


def ensure_data_dir(data_dir: Path = DATA_DIR) -> None:
    """First run: seed backend/data/ (your show, git-ignored) from the shipped
    examples. Never touches an existing data dir."""
    if (data_dir / "devices.json").exists() or not EXAMPLE_DIR.is_dir():
        return
    import shutil
    shutil.copytree(EXAMPLE_DIR, data_dir, dirs_exist_ok=True)


class Store:
    def __init__(self, data_dir: Path = DATA_DIR):
        if data_dir == DATA_DIR:
            ensure_data_dir(data_dir)
        self.profiles: dict[str, FixtureProfile] = {}
        for p in sorted((data_dir / "profiles").glob("*.json")):
            prof = FixtureProfile.model_validate_json(p.read_text())
            self.profiles[prof.id] = prof
        self.devices: dict[str, Device] = {}
        for d in json.loads((data_dir / "devices.json").read_text()):
            dev = Device.model_validate(d)
            self.devices[dev.id] = dev
        self.scenes: dict[str, Scene] = {}
        sdir = data_dir / "scenes"
        if sdir.is_dir():
            for s in sorted(sdir.glob("*.json")):
                sc = Scene.model_validate_json(s.read_text())
                self.scenes[sc.id] = sc
        self._scenes_dir = sdir
        self._devices_file = data_dir / "devices.json"

    def save_devices(self) -> None:
        data = [d.model_dump(exclude_none=True) for d in self.devices.values()]
        self._devices_file.write_text(json.dumps(data, indent=2))

    def save_scene(self, scene: Scene) -> None:
        self.scenes[scene.id] = scene
        self._scenes_dir.mkdir(parents=True, exist_ok=True)
        (self._scenes_dir / f"{scene.id}.json").write_text(scene.model_dump_json(indent=2))

    def delete_scene(self, sid: str) -> None:
        self.scenes.pop(sid, None)
        f = self._scenes_dir / f"{sid}.json"
        if f.exists():
            f.unlink()

    def device(self, device_id: str) -> Device | None:
        return self.devices.get(device_id)

    def profile_for(self, device: Device) -> FixtureProfile | None:
        return self.profiles.get(device.profile_id) if device.profile_id else None
