"""
Agentic Memory Architecture: Observe -> State -> Invalidate -> Replay.

Gives agents persistent, auditable siting decisions across sessions. Two
data sources are supported for the "dossier snapshot" bound to each
observation:

  - fetch_snapshot (default, recommended): calls the well-documented
    POST /v1/fetch with a preset (default "site_selection"). Stable,
    public, in Mireye's docs index.

  - site_snapshot (experimental): calls POST /v1/sites + GET /v1/sites/{id}.
    These exist in Mireye's OpenAPI spec but are NOT in their public docs
    index (docs.mireye.ai/llms.txt) as of this build — likely unreleased
    or beta. Use at your own risk; the request/response shape here is a
    best guess from the untyped `additionalProperties: true` schema, not
    a documented contract. Verify against a real call before relying on it.
"""
from __future__ import annotations

from app.mireye_client import MireyeClient
from app.workspace.store import WorkspaceStore

DEFAULT_PRESET = "site_selection"


class WorkspaceEngine:
    def __init__(self, store: WorkspaceStore | None = None, client: MireyeClient | None = None,
                 snapshot_mode: str = "fetch"):
        self.store = store or WorkspaceStore()
        self.client = client or MireyeClient()
        if snapshot_mode not in ("fetch", "site"):
            raise ValueError("snapshot_mode must be 'fetch' or 'site'")
        self.snapshot_mode = snapshot_mode

    def open(self, workspace_id: str, label: str = "") -> None:
        self.store.create_workspace(workspace_id, label)

    async def _snapshot(self, local_key: str, lat: float | None, lng: float | None,
                         address: str | None, workspace_id: str) -> dict:
        if self.snapshot_mode == "fetch":
            return await self.client.fetch(lat=lat, lng=lng, address=address, preset=DEFAULT_PRESET)

        # experimental /v1/sites path
        site_id = self.store.get_site_id(workspace_id, local_key)
        if not site_id:
            # NOTE: real Mireye site payload shape is unconfirmed (undocumented
            # endpoint). This assumes a GeoJSON Point is acceptable; if the real
            # API requires a Polygon, build a small buffer here instead.
            geometry = {"type": "Point", "coordinates": [lng, lat]} if lat is not None else None
            reg = await self.client.register_site({"geometry": geometry, "address": address})
            site_id = reg.get("site_id") or reg.get("id")
            if not site_id:
                raise RuntimeError(
                    f"register_site() response didn't contain a recognizable site_id: {reg}. "
                    "Inspect the raw response and adjust WorkspaceEngine._snapshot()."
                )
            self.store.register_site_id(workspace_id, local_key, site_id)
        return await self.client.get_site(site_id)

    async def observe(self, workspace_id: str, local_key: str, status: str, justification: str,
                       lat: float | None = None, lng: float | None = None,
                       address: str | None = None) -> dict:
        """Bind a sighting + justification to a site with an immutable dossier snapshot."""
        snapshot = await self._snapshot(local_key, lat, lng, address, workspace_id)
        version = self.store.observe(workspace_id, local_key, status, justification, snapshot)
        return {"local_key": local_key, "version": version, "status": status}

    def state(self, workspace_id: str) -> dict:
        rows = self.store.state(workspace_id)
        return {
            "workspace_id": workspace_id,
            "shortlisted": [r for r in rows if r["status"] == "shortlisted"],
            "candidates": [r for r in rows if r["status"] == "candidate"],
            "rejected": [r for r in rows if r["status"] == "rejected"],
            "rejection_ledger_size": len([r for r in rows if r["status"] == "rejected"]),
        }

    async def invalidate_check(self, workspace_id: str) -> list[dict]:
        """Compare stored dossier snapshots against live data; log + return diffs."""
        stale = []
        for row in self.store.state(workspace_id):
            snap = row["dossier_snapshot"]
            snap_fields = snap.get("fields", {})
            lat, lng = snap.get("lat"), snap.get("lng")
            if lat is None or lng is None:
                continue  # can't re-fetch without a location
            current = await self.client.fetch(lat=lat, lng=lng, preset=DEFAULT_PRESET)
            current_fields = current.get("fields", {})
            for field_name, old_record in snap_fields.items():
                new_record = current_fields.get(field_name)
                if not new_record or new_record.get("status") != "ok":
                    continue
                if new_record.get("value") != old_record.get("value"):
                    self.store.log_staleness(
                        workspace_id, row["local_key"], field_name,
                        old_record.get("value"), new_record.get("value"),
                    )
                    stale.append({
                        "local_key": row["local_key"], "field": field_name,
                        "old_value": old_record.get("value"), "new_value": new_record.get("value"),
                    })
        return stale

    def replay(self, workspace_id: str, as_of_ts: float) -> dict:
        return {"workspace_id": workspace_id, "as_of": as_of_ts,
                "state": self.store.replay(workspace_id, as_of_ts)}

    def history(self, workspace_id: str, local_key: str) -> list[dict]:
        return self.store.history(workspace_id, local_key)