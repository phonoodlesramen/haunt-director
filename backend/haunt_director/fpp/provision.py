"""Pi-local provisioning: make sure the overlay model and a channel output
exist in FPP's config, then bounce fppd — with a guarded auto-revert so a bad
config can never leave fppd crash-looping (learned the hard way, 23 Sep).

Runs ON the Pi (needs filesystem access to /home/fpp/media/config, which is
0700 root:fpp — so writes go through `sudo`). Import and call ensure_all().
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request

CFG_DIR = "/home/fpp/media/config"
MODEL_FILE = f"{CFG_DIR}/model-overlays.json"
UNIV_FILE = f"{CFG_DIR}/co-universes.json"


def _sudo(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True)


def _write_root_file(path: str, text: str) -> None:
    # tee as root, then hand the file to the fpp user
    subprocess.run(["sudo", "-n", "tee", path], input=text, capture_output=True, text=True)
    _sudo("chown", "fpp:fpp", path)


def _fppd_ready(timeout_s: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1/api/fppd/status", timeout=timeout_s) as r:
            return r.status == 200
    except Exception:
        return False


def _restart_fppd_guarded(wait_s: float = 24.0) -> bool:
    _sudo("systemctl", "reset-failed", "fppd")
    _sudo("systemctl", "restart", "fppd")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(4)
        if _fppd_ready():
            return True
    return False


def _model_entry(name: str, start: int, count: int) -> dict:
    return {"Name": name, "Type": "Channel", "StartChannel": start, "ChannelCount": count,
            "StringCount": 1, "StrandsPerString": 1, "ChannelCountPerNode": 1,
            "Orientation": "horizontal", "StartCorner": "TL"}


def _model_config(name: str, start: int, count: int) -> str:
    return json.dumps({"autoCreate": False, "models": [_model_entry(name, start, count)]})


def merged_models(prev_text: str | None, name: str, start: int, count: int) -> str | None:
    """FPP's overlay-model config with OUR model added or corrected and every
    other model left exactly as it was. None means nothing needs to change."""
    want = _model_entry(name, start, count)
    try:
        cur = json.loads(prev_text) if prev_text else None
    except ValueError:
        cur = None
    if not isinstance(cur, dict):
        return _model_config(name, start, count)
    models = [m for m in (cur.get("models") or []) if isinstance(m, dict)]
    idx = [i for i, m in enumerate(models) if m.get("Name") == name]
    if len(idx) == 1 and all(models[idx[0]].get(k) == v for k, v in want.items()):
        return None                                   # already right (extra FPP keys are fine)
    if idx:
        models[idx[0]] = {**models[idx[0]], **want}   # fix ours in place, keep FPP's extra keys
        models = [m for i, m in enumerate(models) if i == idx[0] or m.get("Name") != name]
    else:
        models.append(want)
    new = dict(cur)
    new["models"] = models
    new.setdefault("autoCreate", False)
    return json.dumps(new)


def _e131_config(universe: int, start: int, count: int) -> str:
    return json.dumps({"channelOutputs": [{
        "type": "universes", "enabled": 1, "startChannel": start, "channelCount": count,
        "universes": [{"active": 1, "description": "Haunt Director", "id": universe,
                       "type": 0, "priority": 0, "startChannel": start,
                       "channelCount": count, "universeCount": 1, "monitor": 0}]}]})


def _read_root(path: str) -> str | None:
    p = _sudo("cat", path)
    return p.stdout if p.returncode == 0 else None


def ensure_all(model_name: str, start: int, count: int,
               universe: int = 1, with_output: bool = True) -> dict:
    """Idempotently ensure our overlay model (and optionally an E1.31 output)
    exist. Other overlay models are left alone. Only touches config and
    restarts fppd when something is missing or wrong, keeps a .haunt-backup of
    the previous file, and auto-reverts if fppd fails to come back."""
    prev_model = _read_root(MODEL_FILE)
    want_model = merged_models(prev_model, model_name, start, count)
    want_univ = _e131_config(universe, start, count) if with_output else None
    prev_univ = _read_root(UNIV_FILE)
    univ_changed = want_univ is not None and prev_univ != want_univ
    if want_model is None and not univ_changed:
        return {"changed": False, "ok": _fppd_ready()}

    if want_model is not None:
        if prev_model is not None:
            _write_root_file(MODEL_FILE + ".haunt-backup", prev_model)
        _write_root_file(MODEL_FILE, want_model)
    if univ_changed:
        if prev_univ is not None:
            _write_root_file(UNIV_FILE + ".haunt-backup", prev_univ)
        _write_root_file(UNIV_FILE, want_univ)

    if _restart_fppd_guarded():
        return {"changed": True, "ok": True}

    # revert
    if want_model is not None:
        if prev_model is not None:
            _write_root_file(MODEL_FILE, prev_model)
        else:
            _sudo("rm", "-f", MODEL_FILE)
    if univ_changed:
        if prev_univ is not None:
            _write_root_file(UNIV_FILE, prev_univ)
        else:
            _sudo("rm", "-f", UNIV_FILE)
    _restart_fppd_guarded()
    return {"changed": True, "ok": False, "reverted": True}
