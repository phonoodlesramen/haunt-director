"""Load fixture profiles, devices and scenes from the data/ directory, and save them back."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .domain.devices import Device
from .domain.profiles import FixtureProfile
from .domain.scenes import Scene

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "data-example"
log = logging.getLogger("haunt.store")


def ensure_data_dir(data_dir: Path = DATA_DIR) -> None:
    """First run: seed backend/data/ (your show, git-ignored) from the shipped
    examples. Never touches an existing data dir."""
    if (data_dir / "devices.json").exists() or not EXAMPLE_DIR.is_dir():
        return
    import shutil
    # profiles stay in the shipped library (read straight from data-example), so
    # an update can add or fix them; data/profiles is for your own
    shutil.copytree(EXAMPLE_DIR, data_dir, dirs_exist_ok=True,
                    ignore=lambda d, names: ["profiles"] if Path(d) == EXAMPLE_DIR else [])
    (data_dir / "profiles").mkdir(parents=True, exist_ok=True)


def _same_layout(a: FixtureProfile, b: FixtureProfile) -> bool:
    meta = {"tested", "notes"}
    return a.model_dump(exclude=meta) == b.model_dump(exclude=meta)


class Store:
    def __init__(self, data_dir: Path = DATA_DIR, library_dir: Path | None = None):
        if data_dir == DATA_DIR:
            ensure_data_dir(data_dir)
        # Fixture profiles: the shipped library (data-example/profiles, so an
        # update brings new ones to existing installs) plus your own in
        # data/profiles, which win when the ids match.
        lib = EXAMPLE_DIR / "profiles" if library_dir is None else library_dir
        self.profiles: dict[str, FixtureProfile] = {}
        self.profile_source: dict[str, str] = {}
        for src, folder in (("library", lib), ("yours", data_dir / "profiles")):
            if not folder or not Path(folder).is_dir():
                continue
            for p in sorted(Path(folder).glob("*.json")):
                try:
                    prof = FixtureProfile.model_validate_json(p.read_text())
                except Exception:
                    log.warning("skipping unreadable fixture profile %s", p)
                    continue
                lib_prof = self.profiles.get(prof.id) if src == "yours" else None
                if lib_prof is not None and _same_layout(lib_prof, prof):
                    continue                         # an old copy of a library profile: still the library's
                self.profiles[prof.id] = prof
                self.profile_source[prof.id] = src
        self.devices: dict[str, Device] = {}
        for d in json.loads((data_dir / "devices.json").read_text()):
            dev = Device.model_validate(d)
            self.devices[dev.id] = dev
        self.follows: dict[str, str] = {}
        self._rig_changed()
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
        tmp = self._devices_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._devices_file)              # never a half-written rig

    def replace_devices(self, devices: list[Device]) -> None:
        """Swap in a whole new device list (rig editor). The dict is replaced,
        never edited in place, so the output thread always sees one rig or the
        other, never half of each."""
        self.devices = {d.id: d for d in devices}
        self._rig_changed()
        self.save_devices()

    def _rig_changed(self) -> None:
        from .domain.rig import follow_map
        self.follows = follow_map(list(self.devices.values()))

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
