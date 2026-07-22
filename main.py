"""
AI Urban Heat Intelligence Platform — Backend API
Run with: uvicorn main:app --reload --port 8000
"""

import os
import json
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import anthropic

# ------------------------------------------------------------
# Setup
# ------------------------------------------------------------
load_dotenv()  # reads .env automatically — no manual export/set needed

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY env vars before starting.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

app = FastAPI(title="Urban Heat Intelligence API")

# Allow both frontend apps (citizen app + gov dashboard) to hit this freely during hackathon
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten before any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------
# Request/response models
# ------------------------------------------------------------
class InterventionEstimateRequest(BaseModel):
    zone_id: str
    intervention_type: str  # tree_planting | cooling_center | material_change | shade_structure
    quantity: float


class InterventionEstimateResponse(BaseModel):
    zone_id: str
    intervention_type: str
    quantity: float
    estimated_temp_reduction_c: float
    confidence: float
    reasoning: str


class InterventionCreateRequest(BaseModel):
    zone_id: str
    type: str
    quantity: float


class HeatReportRequest(BaseModel):
    lat: float
    lng: float
    description: Optional[str] = None
    reported_temp: Optional[float] = None
    zone_id: Optional[str] = None


class CoolingGapReportRequest(BaseModel):
    lat: float
    lng: float
    category: str  # no_cooling_center | insufficient_capacity | closed_or_inactive | too_far | other
    description: Optional[str] = None
    reporter_contact: Optional[str] = None
    zone_id: Optional[str] = None


class CoolingGapStatusUpdateRequest(BaseModel):
    status: str  # submitted | under_review | action_planned | resolved | dismissed


# ------------------------------------------------------------
# 1. Heat Zones
# ------------------------------------------------------------
@app.get("/heat-zones")
def get_heat_zones():
    """All zones with current risk level + lat/lng coordinates. Frontend map layer hits this first."""
    result = supabase.rpc("get_heat_zones_with_coords").execute()
    return result.data


@app.get("/heat-zones/{zone_id}")
def get_heat_zone_detail(zone_id: str):
    """Zone detail + historical readings for trend chart."""
    zone = (
        supabase.table("heat_zones")
        .select("id, zone_name, current_temp, risk_level, population_density, green_cover_pct, last_updated")
        .eq("id", zone_id)
        .single()
        .execute()
    )
    if not zone.data:
        raise HTTPException(status_code=404, detail="Zone not found")

    # normalize field names/values to match frontend conventions
    zone.data["name"] = zone.data.pop("zone_name")
    if zone.data["risk_level"] == "medium":
        zone.data["risk_level"] = "moderate"

    # attach lat/lng (raw geometry columns aren't JSON-serializable via the table select above)
    centroid = supabase.rpc("get_zone_centroids").execute()
    match = next((z for z in centroid.data if z["id"] == zone_id), None)
    if match:
        zone.data["centroid_lat"] = match["lat"]
        zone.data["centroid_lng"] = match["lng"]

    readings = (
        supabase.table("heat_readings")
        .select("temperature, source, recorded_at")
        .eq("zone_id", zone_id)
        .order("recorded_at", desc=True)
        .limit(30)
        .execute()
    )

    return {"zone": zone.data, "history": readings.data}


# ------------------------------------------------------------
# 2. Cooling Centers (nearest lookup)
# ------------------------------------------------------------
@app.get("/cooling-centers/nearby")
def get_nearby_cooling_centers(
    lat: float = Query(...),
    lng: float = Query(...),
    radius_km: float = Query(5.0, description="Search radius in kilometers"),
):
    """
    Nearest cooling centers using PostGIS. Calls an RPC function (see below)
    because Supabase's REST layer can't do ST_DWithin directly.
    """
    result = supabase.rpc(
        "nearby_cooling_centers",
        {"lat": lat, "lng": lng, "radius_km": radius_km},
    ).execute()
    return result.data


# ------------------------------------------------------------
# 3. Government Dashboard — Rankings
# ------------------------------------------------------------
@app.get("/dashboard/rankings")
def get_rankings():
    """Highest-risk zones first, for the gov dashboard priority list."""
    result = (
        supabase.table("heat_zones")
        .select("id, zone_name, current_temp, risk_level, population_density, green_cover_pct")
        .order("current_temp", desc=True)
        .execute()
    )
    return result.data


# ------------------------------------------------------------
# 4. Intervention impact estimate (calls AI teammate's model)
# ------------------------------------------------------------
# Published reference correlations given to the model as grounding context,
# so estimates are reasoned from real research rather than invented numbers.
# (Illustrative figures drawn from general urban heat island literature —
# cite your own sources in the pitch if judges ask for specifics.)
INTERVENTION_REFERENCE_DATA = """
Reference correlations from urban heat island research (use as grounding, not exact truth):
- Tree canopy cover: each ~10% increase in neighborhood tree canopy cover is associated with
  roughly 0.5-1.0°C reduction in local ambient temperature, with diminishing returns at high
  density and stronger effect in already low-canopy areas.
- Cooling centers: do not reduce ambient outdoor temperature; they reduce heat-health risk by
  giving people access to a cooled space. Impact should be framed as risk mitigation, not °C reduction.
- Reflective/light-colored building materials (cool roofs/pavements): replacing dark surfaces with
  high-albedo materials over a meaningful share of a zone's surface area can reduce local surface
  and near-surface air temperature by roughly 1-2°C, with effect size scaling with the fraction of
  surface area treated.
- Shade structures (e.g., street canopies, pergolas): reduce perceived and localized temperature
  in the immediate shaded area, with modest effect on broader zone-level ambient temperature
  (typically under 1°C at zone scale) but larger effect on human comfort/heat exposure.
"""


def call_claude_for_estimate(zone: dict, req: "InterventionEstimateRequest") -> dict:
    """Ask Claude to reason over the zone's real data + reference correlations."""
    prompt = f"""You are an urban heat mitigation analyst. Estimate the likely impact of a proposed
intervention using the reference correlations below and the zone's actual current data.

{INTERVENTION_REFERENCE_DATA}

Zone data:
- Name: {zone['zone_name']}
- Current temperature: {zone['current_temp']}°C
- Current green cover: {zone['green_cover_pct']}%
- Population density: {zone['population_density']} people/km^2
- Current risk level: {zone['risk_level']}

Proposed intervention:
- Type: {req.intervention_type}
- Quantity: {req.quantity} (trees for tree_planting, % surface area for material_change,
  number of structures for shade_structure, capacity for cooling_center)

Respond with ONLY a JSON object, no other text, in this exact shape:
{{
  "estimated_temp_reduction_c": <number, 0 if intervention does not reduce ambient temp>,
  "confidence": <number between 0 and 1>,
  "reasoning": "<one or two sentence explanation grounded in the reference data above>"
}}"""

    response = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # strip markdown code fences if the model adds them despite instructions
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


@app.post("/interventions/estimate", response_model=InterventionEstimateResponse)
def estimate_intervention_impact(req: InterventionEstimateRequest):
    """
    Calls Claude to reason over the zone's real data plus published urban-heat
    correlations, rather than using a fixed multiplier. Falls back to a simple
    heuristic if no ANTHROPIC_API_KEY is configured, so the endpoint never
    hard-fails during a demo.
    """
    zone = supabase.table("heat_zones").select("*").eq("id", req.zone_id).single().execute()
    if not zone.data:
        raise HTTPException(status_code=404, detail="Zone not found")

    if claude_client is not None:
        try:
            result = call_claude_for_estimate(zone.data, req)
            return InterventionEstimateResponse(
                zone_id=req.zone_id,
                intervention_type=req.intervention_type,
                quantity=req.quantity,
                estimated_temp_reduction_c=round(float(result["estimated_temp_reduction_c"]), 2),
                confidence=round(float(result["confidence"]), 2),
                reasoning=result["reasoning"],
            )
        except Exception as e:
            # Don't let a flaky API call break the demo — fall through to heuristic below
            print(f"Claude estimate failed, falling back to heuristic: {e}")

    # --- Fallback heuristic (used only if no API key set or Claude call fails) ---
    impact_per_unit = {
        "tree_planting": 0.012,
        "cooling_center": 0.0,
        "material_change": 0.02,
        "shade_structure": 0.015,
    }
    rate = impact_per_unit.get(req.intervention_type, 0.01)
    estimated_reduction = round(req.quantity * rate, 2)

    return InterventionEstimateResponse(
        zone_id=req.zone_id,
        intervention_type=req.intervention_type,
        quantity=req.quantity,
        estimated_temp_reduction_c=estimated_reduction,
        confidence=0.5,
        reasoning="Estimated using a fallback heuristic (AI model unavailable).",
    )


@app.post("/interventions")
def create_intervention(req: InterventionCreateRequest):
    """Persist a proposed intervention (after estimate step)."""
    estimate = estimate_intervention_impact(
        InterventionEstimateRequest(
            zone_id=req.zone_id, intervention_type=req.type, quantity=req.quantity
        )
    )
    result = (
        supabase.table("interventions")
        .insert(
            {
                "zone_id": req.zone_id,
                "type": req.type,
                "quantity": req.quantity,
                "estimated_impact_c": estimate.estimated_temp_reduction_c,
                "confidence": estimate.confidence,
                "status": "proposed",
            }
        )
        .execute()
    )
    return result.data


@app.get("/interventions/{zone_id}")
def get_zone_interventions(zone_id: str):
    result = supabase.table("interventions").select("*").eq("zone_id", zone_id).execute()
    return result.data


# ------------------------------------------------------------
# 6. Cooling Gap Reports — citizens flag missing/inadequate cooling infrastructure
# ------------------------------------------------------------
@app.post("/cooling-gap-reports")
def submit_cooling_gap_report(req: CoolingGapReportRequest):
    """
    Citizen reports a location with no cooling center, or an inadequate one
    (too small, closed, too far). Zone is auto-detected from lat/lng if not given.
    """
    valid_categories = {"no_cooling_center", "insufficient_capacity", "closed_or_inactive", "too_far", "other"}
    if req.category not in valid_categories:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {', '.join(valid_categories)}",
        )

    result = supabase.rpc(
        "insert_cooling_gap_report",
        {
            "p_lat": req.lat,
            "p_lng": req.lng,
            "p_category": req.category,
            "p_description": req.description,
            "p_reporter_contact": req.reporter_contact,
            "p_zone_id": req.zone_id,
        },
    ).execute()
    return result.data


@app.get("/cooling-gap-reports")
def list_cooling_gap_reports(status: Optional[str] = Query(None, description="Filter by status")):
    """All cooling gap reports, optionally filtered by status. For gov review queue."""
    query = supabase.table("cooling_gap_reports").select("*").order("created_at", desc=True)
    if status:
        query = query.eq("status", status)
    result = query.execute()
    return result.data


@app.get("/dashboard/cooling-gaps")
def get_cooling_gap_summary():
    """
    Zones ranked by number of open (unresolved) cooling gap reports — highest first.
    This is the government's 'action needed' priority list for cooling infrastructure.
    """
    result = supabase.rpc("get_cooling_gap_summary").execute()
    return result.data


@app.patch("/cooling-gap-reports/{report_id}")
def update_cooling_gap_status(report_id: str, req: CoolingGapStatusUpdateRequest):
    """Gov marks a report as under review / action planned / resolved / dismissed."""
    valid_statuses = {"submitted", "under_review", "action_planned", "resolved", "dismissed"}
    if req.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(valid_statuses)}",
        )

    result = (
        supabase.table("cooling_gap_reports")
        .update({"status": req.status})
        .eq("id", report_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Report not found")
    return result.data


# ------------------------------------------------------------
# 7. Citizen heat reports (optional feature)
# ------------------------------------------------------------
@app.post("/reports")
def submit_heat_report(req: HeatReportRequest):
    result = supabase.rpc(
        "insert_heat_report",
        {
            "p_lat": req.lat,
            "p_lng": req.lng,
            "p_description": req.description,
            "p_reported_temp": req.reported_temp,
            "p_zone_id": req.zone_id,
        },
    ).execute()
    return result.data


# ------------------------------------------------------------
# Health check
# ------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}