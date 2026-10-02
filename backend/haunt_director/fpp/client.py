"""Async HTTP client for Falcon Player -- READ-ONLY (health / model state).

ALL light and fog output goes through haunt_director/output.py (one writer,
one frame per tick). The range-write helpers that used to live here were
removed on purpose: merged ranges with shifting boundaries pile up in FPP's
append-only range list and override fresh values, and a "delete" of a range
FPP does not hold creates a new one at 255 (on channel 49 that is fog ON).

Original notes (FPP 9.5):

Everything here is verified against a live FPP 9.5 install — see
docs/how-it-works.md. Key facts encoded here:

  * The manual-DMX primitive is a **Pixel Overlay Model**; the model must exist
    and be Enabled, and a channel output must be configured, or writes never
    reach the wire.
  * Range/state values go in the JSON **body**, not the URL path:
      PUT /api/overlays/model/{m}/state   {"State":1}       (0/1/2/3)
      PUT /api/overlays/range/{a}-{b}      {"Value":N}
      PUT /api/overlays/range/{a}-{b}      {"delete":true}   (exact release)
      PUT /api/overlays/range              {"deleteAll":true}
  * GET /api/overlays/model/{m}/data does NOT reflect live output — do not use
    it to confirm what is on the wire.
"""
from __future__ import annotations

import httpx

STATE_DISABLED, STATE_ENABLED, STATE_TRANSPARENT, STATE_TRANSPARENT_RGB = 0, 1, 2, 3


class FppError(RuntimeError):
    """An FPP API call failed or returned an unexpected result."""


class FppClient:
    def __init__(self, base_url: str, model_name: str, timeout_s: float = 6.0):
        self._base = base_url.rstrip("/")
        self.model = model_name
        self._http = httpx.AsyncClient(base_url=self._base, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- low-level ---------------------------------------------------------
    async def _get(self, path: str) -> httpx.Response:
        try:
            return await self._http.get(path)
        except httpx.HTTPError as e:
            raise FppError(f"GET {path} failed: {e}") from e

    async def _put(self, path: str, json: dict) -> dict:
        try:
            r = await self._http.put(path, json=json)
        except httpx.HTTPError as e:
            raise FppError(f"PUT {path} failed: {e}") from e
        if r.status_code != 200:
            raise FppError(f"PUT {path} -> HTTP {r.status_code}: {r.text[:120]}")
        try:
            body = r.json()
        except ValueError:
            return {"raw": r.text}
        if isinstance(body, dict) and body.get("Status") not in (None, "OK"):
            raise FppError(f"PUT {path} -> {body}")
        return body

    # --- health ------------------------------------------------------------
    async def health(self) -> dict:
        """Return a compact health dict; raises FppError if FPP is unreachable."""
        r = await self._get("/api/fppd/status")
        if r.status_code != 200:
            raise FppError(f"fppd status HTTP {r.status_code}")
        d = r.json()
        return {
            "ok": True,
            "mode": d.get("mode_name"),
            "status": d.get("status_name"),
            "outputs_enabled": bool(d.get("channelOutputsEnabled")),
            "inputs_enabled": d.get("channelInputsEnabled"),   # DDP/E1.31 input (needed for DDP transport)
            "warnings": d.get("warnings", []),
        }

    async def model_exists(self) -> bool:
        r = await self._get("/api/models?simple=true")
        return r.status_code == 200 and self.model in r.json()

    async def model_active(self) -> bool:
        r = await self._get(f"/api/overlays/model/{self.model}")
        return r.status_code == 200 and bool(r.json().get("isActive"))
