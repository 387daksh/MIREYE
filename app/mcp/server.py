"""
Model Context Protocol (MCP) tool server & Agent Tool Definitions.

Exposes native AI agent tools for autonomous spatial screening, grid modeling,
and persistent workspace memory operations.
"""
from __future__ import annotations

import json
from typing import Any

from app.discovery.confidence import rank_shortlist_by_confidence, score_site
from app.discovery.spatial import SpatialDiscovery
from app.grid.ici import ICIEngine
from app.mireye_client import MireyeClient
from app.workspace.engine import WorkspaceEngine
from app.workspace.store import WorkspaceStore

mireye_client = MireyeClient()
workspace_store = WorkspaceStore()
workspace_engine = WorkspaceEngine(store=workspace_store, client=mireye_client)
spatial_discovery = SpatialDiscovery()
ici_engine = ICIEngine()

# -----------------------------------------------------------------------------
# Tool Definitions & Schemas
# -----------------------------------------------------------------------------
TOOLS_REGISTRY = [
    {
        "name": "screen_parcels",
        "description": "Inverse candidate search: Scan candidate parcels matching spatial, terrain, environmental, and grid constraints.",
        "parameters": {
            "type": "object",
            "properties": {
                "min_acreage": {"type": "number", "description": "Minimum acreage required"},
                "max_slope_pct": {"type": "number", "description": "Maximum terrain slope percentage (e.g. 5.0 for solar)"},
                "flood_zones": {"type": "array", "items": {"type": "string"}, "description": "Allowed FEMA flood zones (e.g. ['X'])"},
                "min_substation_capacity_mw": {"type": "number", "description": "Minimum capacity at nearest substation in MW"},
                "max_distance_to_substation_km": {"type": "number", "description": "Maximum distance to substation in km"},
                "zoning_renewable_only": {"type": "boolean", "description": "Require renewable energy permitted zoning"},
                "limit": {"type": "integer", "description": "Max results to return (default 20)"},
            },
        },
    },
    {
        "name": "get_grid_capacity",
        "description": "Analyze substation capacity dynamics (SCD), firm vs contested headroom, FERC queue attrition velocity, and ROW feasibility.",
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude of target site"},
                "lng": {"type": "number", "description": "Longitude of target site"},
                "target_capacity_mw": {"type": "number", "description": "Desired project capacity in MW"},
                "compress_tokens": {"type": "boolean", "description": "Whether to compress output by 80% for context window savings"},
            },
            "required": ["lat", "lng"],
        },
    },
    {
        "name": "verify_parcel",
        "description": "Fetch deep-dive facts, federal data layer provenance, and heuristic confidence scores for a single parcel.",
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude"},
                "lng": {"type": "number", "description": "Longitude"},
                "address": {"type": "string", "description": "Parcel ID or address"},
                "preset": {"type": "string", "description": "Preset name (e.g. 'site_selection', 'terrain', 'grid')"},
            },
        },
    },
    {
        "name": "workspace_observe",
        "description": "Bind an agent observation, status ('shortlisted', 'candidate', 'rejected'), and justification to a parcel with an immutable snapshot.",
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace ID"},
                "local_key": {"type": "string", "description": "Parcel ID or local key"},
                "status": {"type": "string", "enum": ["shortlisted", "candidate", "rejected"], "description": "Decision status"},
                "justification": {"type": "string", "description": "Reasoning / rationale for this status"},
                "lat": {"type": "number", "description": "Latitude"},
                "lng": {"type": "number", "description": "Longitude"},
            },
            "required": ["workspace_id", "local_key", "status", "justification"],
        },
    },
    {
        "name": "workspace_state",
        "description": "Retrieve current workspace state including active shortlisted sites and rejection moat ledger.",
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace ID"},
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "workspace_invalidate",
        "description": "Check for staleness or drift in underlying data strata across all observed sites in the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace ID"},
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "workspace_replay",
        "description": "Reconstruct exact agent intelligence state at any prior timestamp for audit trails.",
        "parameters": {
            "type": "object",
            "properties": {
                "workspace_id": {"type": "string", "description": "Workspace ID"},
                "as_of_ts": {"type": "number", "description": "Epoch timestamp to replay"},
            },
            "required": ["workspace_id", "as_of_ts"],
        },
    },
]


async def execute_tool(tool_name: str, arguments: dict[str, Any]) -> dict:
    """Execute an MCP tool call."""
    if tool_name == "screen_parcels":
        candidates = spatial_discovery.search_candidates(
            min_acreage=arguments.get("min_acreage"),
            max_slope_pct=arguments.get("max_slope_pct"),
            flood_zones=arguments.get("flood_zones"),
            min_substation_capacity_mw=arguments.get("min_substation_capacity_mw"),
            max_distance_to_substation_km=arguments.get("max_distance_to_substation_km"),
            zoning_renewable_only=arguments.get("zoning_renewable_only", False),
            limit=arguments.get("limit", 20),
        )
        ranked = rank_shortlist_by_confidence(candidates)
        return {"candidates_found": len(ranked), "shortlist": ranked}

    elif tool_name == "get_grid_capacity":
        analysis = ici_engine.analyze_interconnection(
            lat=arguments["lat"],
            lng=arguments["lng"],
            target_capacity_mw=arguments.get("target_capacity_mw", 50.0),
        )
        if arguments.get("compress_tokens", False):
            return ici_engine.compress_for_llm(analysis)
        return analysis

    elif tool_name == "verify_parcel":
        dossier = await mireye_client.fetch(
            lat=arguments.get("lat"),
            lng=arguments.get("lng"),
            address=arguments.get("address"),
            preset=arguments.get("preset", "site_selection"),
        )
        confidence = score_site(dossier.get("fields", {}))
        return {"dossier": dossier, "confidence": confidence}

    elif tool_name == "workspace_observe":
        return await workspace_engine.observe(
            workspace_id=arguments["workspace_id"],
            local_key=arguments["local_key"],
            status=arguments["status"],
            justification=arguments["justification"],
            lat=arguments.get("lat"),
            lng=arguments.get("lng"),
        )

    elif tool_name == "workspace_state":
        return workspace_engine.state(arguments["workspace_id"])

    elif tool_name == "workspace_invalidate":
        stale = await workspace_engine.invalidate_check(arguments["workspace_id"])
        return {"stale_fields_count": len(stale), "stale_fields": stale}

    elif tool_name == "workspace_replay":
        return workspace_engine.replay(arguments["workspace_id"], arguments["as_of_ts"])

    else:
        raise ValueError(f"Unknown tool: {tool_name}")
