"""
AI Urban Heat Intelligence Platform — Backend API
Run with: uvicorn main:app --reload --port 8000
"""

import os
import json
from datetime import datetime
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler

import update_weather  # reuses the exact same logic as the manual script

# ------------------------------------------------------------
# Setup
# ------------------------------------------------------------
load_dotenv()  # reads .env automatically — no manual export/set needed

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")  # optional — enables live-traffic routing

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
# Automatic live weather refresh — runs update_weather.main() on a
# schedule for as long as the backend process is up. No separate
# terminal, cron job, or external service needed. Adjust the interval
# below to taste (5-15 min is reasonable for a demo without hammering
# OpenWeatherMap's free tier).
# ------------------------------------------------------------
scheduler = BackgroundScheduler()


def _run_weather_refresh():
    try:
        print("[scheduler] Running scheduled weather refresh...")
        update_weather.main()
    except Exception as e:
        # Never let a scheduled refresh failure crash the whole server
        print(f"[scheduler] Weather refresh failed: {e}")


@app.on_event("startup")
def start_weather_scheduler():
    # BUG FIX: next_run_time=None does NOT mean "use default scheduling" —
    # in APScheduler it explicitly means "add this job PAUSED." The job was
    # being registered with a 10-minute interval trigger but never actually
    # firing on its own, since nothing ever resumed it. Passing
    # datetime.now() instead makes it run immediately on startup (so data
    # isn't stale for up to 10 minutes after every deploy/restart) and then
    # every 10 minutes after that via the interval trigger.
    scheduler.add_job(_run_weather_refresh, "interval", minutes=10, next_run_time=datetime.now())
    scheduler.start()
    print("[scheduler] Live weather refresh scheduled every 10 minutes, starting now.")


@app.on_event("shutdown")
def stop_weather_scheduler():
    scheduler.shutdown(wait=False)


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
    estimated_reduction_c: float
    confidence: float
    reasoning: str


class InterventionCreateRequest(BaseModel):
    zone_id: str
    intervention_type: str
    quantity: float
    estimated_reduction_c: float
    confidence: float
    reasoning: str


class HeatReportRequest(BaseModel):
    lat: float
    lng: float
    description: Optional[str] = None
    estimated_temp_c: Optional[float] = None
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

    # match ZoneDetailPanel.tsx's exact expected field names: timestamp + temp_c
    history = [
        {
            "temp_c": float(r["temperature"]) if r["temperature"] is not None else None,
            "source": r["source"],
            "timestamp": r["recorded_at"],
        }
        for r in readings.data
    ]

    # Real trend from actual historical readings — compares the average of
    # the 3 most recent readings against the average of the 3 before that.
    # Not a forecast, just an honest "is it currently trending up/down."
    trend = "stable"
    valid_temps = [h["temp_c"] for h in history if h["temp_c"] is not None]
    if len(valid_temps) >= 4:
        recent = valid_temps[:3]
        older = valid_temps[3:6] or valid_temps[3:]
        if older:
            diff = (sum(recent) / len(recent)) - (sum(older) / len(older))
            if diff > 0.3:
                trend = "rising"
            elif diff < -0.3:
                trend = "falling"
    zone.data["trend"] = trend

    # match ZoneDetailPanel.tsx's expected field name: current_temp_c
    zone.data["current_temp_c"] = (
        float(zone.data.pop("current_temp")) if zone.data.get("current_temp") is not None else None
    )
    if zone.data.get("green_cover_pct") is not None:
        zone.data["green_cover_pct"] = float(zone.data["green_cover_pct"])

    # flat shape: zone fields + history alongside, not nested under a "zone" key
    return {**zone.data, "history": history}


# ------------------------------------------------------------
# 2. Cooling Centers (nearest lookup)
# ------------------------------------------------------------
def _lookup_cooling_centers(lat: float, lng: float, radius_km: float = 5.0) -> list[dict]:
    """
    Shared lookup used by both GET /cooling-centers/nearby and the
    assistant's get_nearby_cooling_centers tool, so there's exactly one
    place this logic lives instead of two copies that could drift apart.

    Wrapped in try/except so a broken/missing RPC (PostGIS not enabled,
    function renamed, table empty causing an unexpected shape) returns an
    empty list instead of raising — same honest-fallback pattern used for
    hospitals.
    """
    try:
        result = supabase.rpc(
            "nearby_cooling_centers",
            {"lat": lat, "lng": lng, "radius_km": radius_km},
        ).execute()
        centers = result.data or []
    except Exception as e:
        print(f"Cooling centers lookup failed: {e}")
        return []

    print(f"[DEBUG] cooling centers query lat={lat} lng={lng} radius_km={radius_km}: {len(centers)} results")

    # Cap to nearest 15, same rationale as hospitals — if the RPC's own
    # distance sort isn't guaranteed, sort here too before slicing.
    def _dist(c: dict) -> float:
        return ((c.get("lat", lat) - lat) ** 2 + (c.get("lng", lng) - lng) ** 2) ** 0.5

    centers.sort(key=_dist)
    return centers[:15]


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
    return _lookup_cooling_centers(lat, lng, radius_km)


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
    rankings = []
    for i, z in enumerate(result.data, start=1):
        risk = "moderate" if z["risk_level"] == "medium" else z["risk_level"]
        rankings.append(
            {
                "zone_id": z["id"],
                "zone_name": z["zone_name"],
                "risk_level": risk,
                "current_temp_c": float(z["current_temp"]) if z["current_temp"] is not None else None,
                "rank": i,
            }
        )
    return rankings


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
  "estimated_reduction_c": <number, 0 if intervention does not reduce ambient temp>,
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
                estimated_reduction_c=round(float(result["estimated_reduction_c"]), 2),
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
        estimated_reduction_c=estimated_reduction,
        confidence=0.5,
        reasoning="Estimated using a fallback heuristic (AI model unavailable).",
    )


@app.post("/interventions")
def create_intervention(req: InterventionCreateRequest):
    """
    Persist a proposed intervention. Uses the estimate values the frontend
    already computed via /interventions/estimate and is now confirming —
    does NOT re-call Claude, so the saved reasoning matches exactly what the
    user saw before saving (and avoids a redundant AI call).
    """
    result = (
        supabase.table("interventions")
        .insert(
            {
                "zone_id": req.zone_id,
                "type": req.intervention_type,
                "quantity": req.quantity,
                "estimated_impact_c": req.estimated_reduction_c,
                "confidence": req.confidence,
                "reasoning": req.reasoning,
                "status": "proposed",
            }
        )
        .execute()
    )
    return [_to_intervention_record(r) for r in result.data]


def _to_intervention_record(row: dict) -> dict:
    """Map raw interventions table columns to the frontend's InterventionRecord shape."""
    return {
        "id": row["id"],
        "zone_id": row["zone_id"],
        "intervention_type": row["type"],
        "quantity": row["quantity"],
        "estimated_reduction_c": float(row["estimated_impact_c"]) if row.get("estimated_impact_c") is not None else None,
        "confidence": float(row["confidence"]) if row.get("confidence") is not None else None,
        "reasoning": row.get("reasoning"),
        "created_at": row["proposed_at"],
    }


@app.get("/interventions/{zone_id}")
def get_zone_interventions(zone_id: str):
    result = supabase.table("interventions").select("*").eq("zone_id", zone_id).execute()
    return [_to_intervention_record(r) for r in result.data]


# ------------------------------------------------------------
# 5. Zone Risk Reasoning — explainable factor breakdown per zone
#
# Same pattern as call_claude_for_estimate: ask Claude to reason over the
# zone's actual data, not invented weights. Falls back to a deterministic
# heuristic (based on real green_cover_pct/population_density/current_temp)
# if no ANTHROPIC_API_KEY is set, so the endpoint never hard-fails.
#
# HONEST SCOPE NOTE: this is Claude reasoning over the zone's real inputs
# with weights that sum to ~100 — it is NOT a trained SHAP/Grad-CAM
# explainability model. Label it in the UI as "AI-generated reasoning",
# not "SHAP verified", since that specific claim wouldn't be accurate here.
# ------------------------------------------------------------
class ReasoningFactor(BaseModel):
    label: str
    weight: int  # 0-100, factors for a zone should sum to ~100


class ZoneReasoningResponse(BaseModel):
    zone_id: str
    zone_name: str
    factors: list[ReasoningFactor]
    summary: str


def compute_reasoning_factors(zone: dict) -> list[dict]:
    """
    Deterministic, real-math weight calculation from the zone's actual data —
    no LLM involved in the numbers themselves. This is what makes the
    percentages genuinely different per zone instead of an LLM's generic
    guess at "what sounds about right."

    Baselines below (55% green cover target, 27°C ambient baseline) are
    reasonable defaults for a tropical city — tune them if your dataset's
    actual min/max ranges differ meaningfully.
    """
    green = zone.get("green_cover_pct") or 0
    density = zone.get("population_density") or 0
    temp = zone.get("current_temp") or 0

    green_deficit = max(0.0, 55 - green)                # bigger gap from target = bigger factor
    density_factor = min(100.0, density / 300)           # scaled; adjust divisor to your city's real density range
    temp_factor = max(0.0, (temp - 27) * 4)              # degrees above a mild-climate baseline
    uhi_baseline = 10.0                                   # fixed component for built-environment effects not otherwise measured (surface material, building height) — not derived from a real field, kept small and labeled

    raw = {
        "Green cover deficit": green_deficit,
        "Population density": density_factor,
        "Ambient temperature": temp_factor,
        "Unmeasured built-environment factors": uhi_baseline,
    }
    total = sum(raw.values()) or 1
    return [{"label": label, "weight": round(v / total * 100)} for label, v in raw.items()]


def call_claude_for_reasoning_summary(zone: dict, factors: list[dict]) -> str:
    """Claude only writes the one-sentence explanation — grounded in the exact,
    already-computed weights above, so the text can't invent different numbers
    than what's actually shown."""
    factor_lines = "\n".join(f"- {f['label']}: {f['weight']}%" for f in factors)
    prompt = f"""Write ONE sentence explaining why {zone['zone_name']} has {zone['risk_level']} heat risk,
using exactly these already-computed contributing factors (do not invent different numbers):

{factor_lines}

Zone context: {zone['current_temp']}°C, {zone['green_cover_pct']}% green cover,
{zone['population_density']} people/km^2.

Respond with ONLY the sentence, no preamble, no quotes."""

    response = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


@app.get("/heat-zones/{zone_id}/reasoning", response_model=ZoneReasoningResponse)
def get_zone_reasoning(zone_id: str):
    zone = supabase.table("heat_zones").select("*").eq("id", zone_id).single().execute()
    if not zone.data:
        raise HTTPException(status_code=404, detail="Zone not found")

    factors = compute_reasoning_factors(zone.data)

    summary = "Estimated using computed factor weights (AI narrative unavailable)."
    if claude_client is not None:
        try:
            summary = call_claude_for_reasoning_summary(zone.data, factors)
        except Exception as e:
            print(f"Claude summary failed, using fallback text: {e}")

    return ZoneReasoningResponse(
        zone_id=zone_id,
        zone_name=zone.data["zone_name"],
        factors=[ReasoningFactor(**f) for f in factors],
        summary=summary,
    )


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
    result = supabase.rpc("get_cooling_gap_reports_with_coords", {"p_status": status}).execute()
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
            "p_reported_temp": req.estimated_temp_c,
            "p_zone_id": req.zone_id,
        },
    ).execute()
    return result.data


# ------------------------------------------------------------
# 8. Route heat-safety check (simplified "coolest route" heuristic)
#
# HONEST SCOPE NOTE: this does NOT compute a true shade-optimized route —
# that would need a real routing engine (e.g. Mapbox Directions) plus
# per-street tree-canopy/building-shadow data we don't have. Instead, this
# samples points along the straight-line path between origin and
# destination, finds the nearest zone's risk level for each sample, and
# reports which zones the route passes near plus an overall safety label.
# It's an honest, useful signal ("this path crosses 2 high-risk zones") —
# not a routing algorithm. Say so plainly if asked in the demo.
# ------------------------------------------------------------
class RouteSafetyRequest(BaseModel):
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float
    samples: int = 8  # number of points checked along the straight-line path


@app.post("/route/safety-check")
def check_route_safety(req: RouteSafetyRequest):
    zones = supabase.rpc("get_heat_zones_with_coords").execute().data
    if not zones:
        raise HTTPException(status_code=404, detail="No zones available to check against")

    def nearest_zone(lat: float, lng: float):
        best, best_dist = None, float("inf")
        for z in zones:
            d = ((z["centroid_lat"] - lat) ** 2 + (z["centroid_lng"] - lng) ** 2) ** 0.5
            if d < best_dist:
                best, best_dist = z, d
        return best

    passed_zones = []
    seen_ids = set()
    n = max(2, req.samples)
    for i in range(n):
        t = i / (n - 1)
        lat = req.origin_lat + (req.dest_lat - req.origin_lat) * t
        lng = req.origin_lng + (req.dest_lng - req.origin_lng) * t
        z = nearest_zone(lat, lng)
        if z and z["id"] not in seen_ids:
            seen_ids.add(z["id"])
            passed_zones.append({"zone_id": z["id"], "name": z["name"], "risk_level": z["risk_level"]})

    risk_rank = {"low": 0, "moderate": 1, "high": 2, "severe": 3}
    worst = max(passed_zones, key=lambda z: risk_rank.get(z["risk_level"], 0)) if passed_zones else None
    high_risk_count = sum(1 for z in passed_zones if z["risk_level"] in ("high", "severe"))

    if not worst:
        overall = "unknown"
    elif worst["risk_level"] in ("high", "severe"):
        overall = "risky"
    elif worst["risk_level"] == "moderate":
        overall = "caution"
    else:
        overall = "safe"

    return {
        "overall_safety": overall,
        "high_risk_zone_count": high_risk_count,
        "zones_passed": passed_zones,
        "note": (
            "Estimated from straight-line sampling against zone risk levels — "
            "not a true shade-routed path."
        ),
    }


# ------------------------------------------------------------
# 9. Executive Reports — saved snapshots, not regenerated live
#
# Unlike the reasoning/estimate endpoints, this deliberately FREEZES data at
# generation time into the `data` jsonb column. A report from last week
# should still show last week's numbers when viewed later, even if zone
# data has since changed — that's the whole point of a "report" vs a live
# dashboard view.
# ------------------------------------------------------------
class GeneratedReportSummary(BaseModel):
    id: str
    title: str
    generated_at: str
    elevated_zone_count: int
    open_gap_count: int


class GeneratedReportDetail(BaseModel):
    id: str
    title: str
    generated_at: str
    data: dict


@app.post("/dashboard/reports/generate", response_model=GeneratedReportDetail)
def generate_report():
    zones = supabase.rpc("get_heat_zones_with_coords").execute().data or []
    elevated = [z for z in zones if z.get("risk_level") in ("high", "extreme", "severe")]

    gaps = supabase.rpc("get_cooling_gap_summary").execute().data or []
    open_gap_total = sum(g.get("open_report_count", 0) for g in gaps)

    top_zone_reasoning = None
    top_zone_result = (
        supabase.table("heat_zones")
        .select("id, zone_name, current_temp, risk_level, population_density, green_cover_pct")
        .order("current_temp", desc=True)
        .limit(1)
        .execute()
    )
    if top_zone_result.data:
        top_zone = top_zone_result.data[0]
        try:
            factors = compute_reasoning_factors(top_zone)
            summary = "Estimated using computed factor weights (AI narrative unavailable)."
            if claude_client is not None:
                summary = call_claude_for_reasoning_summary(top_zone, factors)
            top_zone_reasoning = {"zone_name": top_zone["zone_name"], "factors": factors, "summary": summary}
        except Exception as e:
            print(f"Report generation: reasoning failed: {e}")

    snapshot = {
        "overview": (
            f"{len(zones)} zones monitored. {len(elevated)} at high or extreme risk. "
            f"{open_gap_total} open cooling-gap reports."
        ),
        "elevated_zones": [
            {"id": z.get("id"), "name": z.get("name"), "current_temp_c": z.get("current_temp_c"), "risk_level": z.get("risk_level")}
            for z in elevated
        ],
        "cooling_gaps": gaps[:8],
        "top_zone_reasoning": top_zone_reasoning,
    }

    title = f"Urban Heat Risk Summary — {datetime.utcnow().strftime('%b %d, %Y %H:%M UTC')}"
    result = supabase.table("reports").insert({"title": title, "data": snapshot}).execute()
    row = result.data[0]
    return GeneratedReportDetail(id=row["id"], title=row["title"], generated_at=row["generated_at"], data=row["data"])


@app.get("/dashboard/reports", response_model=list[GeneratedReportSummary])
def list_reports():
    result = supabase.table("reports").select("id, title, generated_at, data").order("generated_at", desc=True).execute()
    summaries = []
    for r in result.data:
        data = r.get("data") or {}
        summaries.append(
            GeneratedReportSummary(
                id=r["id"],
                title=r["title"],
                generated_at=r["generated_at"],
                elevated_zone_count=len(data.get("elevated_zones", [])),
                open_gap_count=sum(g.get("open_report_count", 0) for g in data.get("cooling_gaps", [])),
            )
        )
    return summaries


@app.get("/dashboard/reports/{report_id}", response_model=GeneratedReportDetail)
def get_report(report_id: str):
    result = supabase.table("reports").select("*").eq("id", report_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Report not found")
    row = result.data
    return GeneratedReportDetail(id=row["id"], title=row["title"], generated_at=row["generated_at"], data=row["data"])


# ------------------------------------------------------------
# 10. Heat Safety Assistant — tool-using agent
#
# Instead of pre-fetching a fixed bundle (nearest zone + 3 cooling
# centers) on every message, Claude now decides which real backend
# endpoints it actually needs for the question asked, calls them as
# tools, and only then answers — grounded in whatever it actually
# looked up, nothing invented.
#
# HONEST SCOPE NOTE on check_route_safety: this tool requires real
# destination coordinates. There's no geocoder in this stack to turn a
# typed place name ("the market on 5th street") into lat/lng, so the
# system prompt instructs Claude to ask the user to share/select a
# destination (e.g. tap it on the map) rather than guess coordinates —
# same honesty rule as everywhere else in this file: no invented facts.
#
# HONEST SCOPE NOTE on submit_cooling_gap_report: this is the one tool
# that takes a real action (writes a row to cooling_gap_reports) rather
# than just reading data. The system prompt instructs Claude to only
# call it when the user has clearly and explicitly asked for a report
# to be filed, not merely described a problem in passing.
# ------------------------------------------------------------
class AssistantMessage(BaseModel):
    role: str  # "user" or "assistant"
    text: str


class AssistantMessageRequest(BaseModel):
    message: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    history: Optional[list[AssistantMessage]] = None
    language: Optional[str] = "en"  # "en" or "mm" (Burmese)
    # Optional: if the frontend already has a destination selected (e.g. a
    # hospital/cooling-center pin tapped on the map), pass its coordinates
    # here so check_route_safety can use them without needing a geocoder.
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None


class AssistantMessageResponse(BaseModel):
    reply: str
    zone_context: Optional[dict] = None
    # Populated only when submit_cooling_gap_report was actually called
    # during this turn, so the frontend can show a concrete confirmation
    # (e.g. a toast with the report id) rather than just trusting the text.
    report_submitted: Optional[dict] = None


ASSISTANT_TOOLS = [
    {
        "name": "get_location_context",
        "description": (
            "Get the user's nearest monitored heat zone: name, risk level, "
            "current temperature, green cover %, and trend. Uses the "
            "user's current lat/lng already provided in this conversation "
            "— call this first whenever you need to know the user's "
            "current heat risk and don't already have it from earlier in "
            "this same turn."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_nearby_cooling_centers",
        "description": (
            "Get real cooling centers / water stations near the user's "
            "current location, with distance, hours, capacity, and contact "
            "info. Never invent a cooling center name — always call this "
            "before recommending one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "radius_km": {"type": "number", "description": "Search radius in km. Default 5."}
            },
            "required": [],
        },
    },
    {
        "name": "get_nearby_hospitals",
        "description": (
            "Get real hospitals/clinics near the user's current location, "
            "with distance, phone, and whether they're marked 24/7 "
            "emergency. Use this for heat-stroke or medical-emergency "
            "questions. Never invent a hospital name — always call this "
            "before recommending one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "radius_km": {"type": "number", "description": "Search radius in km. Default 15."}
            },
            "required": [],
        },
    },
    {
        "name": "check_route_safety",
        "description": (
            "Check whether a route from the user's current location to a "
            "destination passes through elevated heat-risk zones. "
            "Requires REAL destination coordinates — never guess or "
            "estimate coordinates for a place name you don't have exact "
            "coordinates for. If you don't have destination coordinates "
            "available, ask the user to share or select the destination "
            "(e.g. tap it on the map) instead of calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dest_lat": {"type": "number"},
                "dest_lng": {"type": "number"},
            },
            "required": ["dest_lat", "dest_lng"],
        },
    },
    {
        "name": "get_zone_trend",
        "description": (
            "Get the recent temperature trend (rising/falling/stable) and "
            "last few readings for a heat zone. If zone_id is omitted, "
            "uses the user's nearest zone (call get_location_context first "
            "if you don't already know it)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string", "description": "Zone id. Omit to use the user's nearest zone."}
            },
            "required": [],
        },
    },
    {
        "name": "submit_cooling_gap_report",
        "description": (
            "File a cooling-gap report at the user's current location on "
            "their behalf. ONLY call this when the user has clearly and "
            "explicitly asked you to submit/file/report something (e.g. "
            "'report that there's no cooling center here', 'file a "
            "report, it's closed'). Do NOT call this just because the "
            "user is describing a problem conversationally — if it's "
            "ambiguous whether they want it submitted, ask them to "
            "confirm first instead of calling this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "no_cooling_center",
                        "insufficient_capacity",
                        "closed_or_inactive",
                        "too_far",
                        "other",
                    ],
                },
                "description": {
                    "type": "string",
                    "description": "Optional extra detail drawn from the user's message.",
                },
            },
            "required": ["category"],
        },
    },
]


def _zone_trend_summary(zone_id: str) -> dict:
    """
    Lightweight standalone version of the trend calculation already used
    in GET /heat-zones/{zone_id} — kept separate rather than importing
    that endpoint's full response (which also does centroid lookups etc.
    the assistant doesn't need) to keep this tool call cheap and focused.
    """
    zone = (
        supabase.table("heat_zones")
        .select("id, zone_name, current_temp, risk_level")
        .eq("id", zone_id)
        .single()
        .execute()
    )
    if not zone.data:
        return {"error": "zone not found"}

    readings = (
        supabase.table("heat_readings")
        .select("temperature, recorded_at")
        .eq("zone_id", zone_id)
        .order("recorded_at", desc=True)
        .limit(6)
        .execute()
    )
    temps = [float(r["temperature"]) for r in readings.data if r["temperature"] is not None]

    trend = "stable"
    if len(temps) >= 4:
        recent = temps[:3]
        older = temps[3:6] or temps[3:]
        if older:
            diff = (sum(recent) / len(recent)) - (sum(older) / len(older))
            if diff > 0.3:
                trend = "rising"
            elif diff < -0.3:
                trend = "falling"

    return {
        "zone_name": zone.data["zone_name"],
        "current_temp_c": zone.data.get("current_temp"),
        "risk_level": zone.data.get("risk_level"),
        "trend": trend,
        "recent_readings_c": temps,
    }


def _dispatch_assistant_tool(name: str, tool_input: dict, req: AssistantMessageRequest, state: dict) -> dict:
    """
    Executes one tool call and returns a JSON-serializable result to feed
    back to Claude. `state` is a small mutable dict the assistant_message
    endpoint uses to collect side effects across the tool-use loop (the
    zone_context to surface in the HTTP response, and confirmation of any
    report that actually got submitted).
    """
    if name == "get_location_context":
        if req.lat is None or req.lng is None:
            return {"error": "no location shared by the user yet"}
        zones = supabase.rpc("get_heat_zones_with_coords").execute().data or []
        nearest = _find_nearest_zone(req.lat, req.lng, zones) if zones else None
        if not nearest:
            return {"error": "no monitored zones available"}
        context = {
            "zone_id": nearest.get("id"),
            "name": nearest.get("name"),
            "risk_level": nearest.get("risk_level"),
            "current_temp_c": nearest.get("current_temp_c"),
            "green_cover_pct": nearest.get("green_cover_pct"),
        }
        state["zone_context"] = context
        state["last_zone_id"] = nearest.get("id")
        return context

    if name == "get_nearby_cooling_centers":
        if req.lat is None or req.lng is None:
            return {"error": "no location shared by the user yet"}
        radius_km = tool_input.get("radius_km", 5.0)
        centers = _lookup_cooling_centers(req.lat, req.lng, radius_km)
        return {"centers": centers, "count": len(centers)}

    if name == "get_nearby_hospitals":
        if req.lat is None or req.lng is None:
            return {"error": "no location shared by the user yet"}
        radius_km = tool_input.get("radius_km", 15.0)
        hospitals = get_nearby_hospitals(lat=req.lat, lng=req.lng, radius_km=radius_km)
        return {"hospitals": hospitals, "count": len(hospitals)}

    if name == "check_route_safety":
        if req.lat is None or req.lng is None:
            return {"error": "no user location shared yet"}
        try:
            result = check_route_safety(
                RouteSafetyRequest(
                    origin_lat=req.lat,
                    origin_lng=req.lng,
                    dest_lat=tool_input["dest_lat"],
                    dest_lng=tool_input["dest_lng"],
                )
            )
            return result
        except HTTPException as e:
            return {"error": e.detail}

    if name == "get_zone_trend":
        zone_id = tool_input.get("zone_id") or state.get("last_zone_id")
        if not zone_id:
            return {"error": "no zone_id available — call get_location_context first, or ask the user for a zone"}
        return _zone_trend_summary(zone_id)

    if name == "submit_cooling_gap_report":
        if req.lat is None or req.lng is None:
            return {"error": "no user location shared yet — can't file a report without a location"}
        try:
            result = submit_cooling_gap_report(
                CoolingGapReportRequest(
                    lat=req.lat,
                    lng=req.lng,
                    category=tool_input["category"],
                    description=tool_input.get("description"),
                    reporter_contact=None,
                    zone_id=state.get("last_zone_id"),
                )
            )
            state["report_submitted"] = {"category": tool_input["category"], "result": result}
            return {"success": True, "result": result}
        except HTTPException as e:
            return {"error": e.detail}

    return {"error": f"unknown tool: {name}"}


def _find_nearest_zone(lat: float, lng: float, zones: list[dict]) -> Optional[dict]:
    best, best_dist = None, float("inf")
    for z in zones:
        d = ((z["centroid_lat"] - lat) ** 2 + (z["centroid_lng"] - lng) ** 2) ** 0.5
        if d < best_dist:
            best, best_dist = z, d
    return best


@app.post("/assistant/message", response_model=AssistantMessageResponse)
def assistant_message(req: AssistantMessageRequest):
    state: dict = {}

    if claude_client is None:
        # Fallback with no LLM at all: same minimal templated behavior as
        # before, real data only, no tool-calling possible without a model.
        zone_context = None
        if req.lat is not None and req.lng is not None:
            zones = supabase.rpc("get_heat_zones_with_coords").execute().data or []
            nearest = _find_nearest_zone(req.lat, req.lng, zones) if zones else None
            if nearest:
                zone_context = {
                    "name": nearest.get("name"),
                    "risk_level": nearest.get("risk_level"),
                    "current_temp_c": nearest.get("current_temp_c"),
                    "green_cover_pct": nearest.get("green_cover_pct"),
                }
        if zone_context:
            reply = (
                f"You're near {zone_context['name']}, currently {zone_context['risk_level']} risk "
                f"at {zone_context['current_temp_c']}°C."
            )
        else:
            reply = "Share your location so I can give a recommendation grounded in your nearest zone's real data."
        return AssistantMessageResponse(reply=reply, zone_context=zone_context)

    language_instruction = (
        "Respond in Burmese (Myanmar language). Use short, simple, grammatically correct "
        "everyday spoken Burmese — the way a person would actually text a friend, not "
        "literary or formal written Burmese, not a word-for-word translation of English "
        "sentence structure. Prefer short independent sentences over long compound ones. "
        "If a Burmese sentence is getting long or complex, split it into two simpler "
        "sentences instead. Double-check subject-verb agreement and particle usage before "
        "finalizing your reply — grammatically broken Burmese is worse than a shorter, "
        "simpler correct sentence."
        if req.language == "mm"
        else "Respond in English."
    )

    location_note = (
        f"\nUser's current location: lat={req.lat}, lng={req.lng}."
        if req.lat is not None and req.lng is not None
        else "\nNo location shared yet — don't assume one; ask for it if a tool needs it."
    )
    dest_note = (
        f"\nA destination is already selected: lat={req.dest_lat}, lng={req.dest_lng} — "
        "use these directly with check_route_safety if the user asks about route safety, "
        "don't ask them to re-share it."
        if req.dest_lat is not None and req.dest_lng is not None
        else ""
    )

    system_prompt = f"""You are a heat-safety assistant for a citizen-facing urban heat app. Be concise
(2-4 sentences per reply), practical, and only state facts backed by a tool call — never invent a
specific zone name, temperature, cooling-center name, or hospital name. {language_instruction}
{location_note}{dest_note}

Use the available tools whenever you need real data to answer accurately. Call get_location_context
before stating the user's current risk/temperature if you don't already have it in this conversation.
Only call submit_cooling_gap_report when the user has clearly asked you to file a report."""

    # Build message history for the Anthropic Messages API
    messages: list[dict] = []
    if req.history:
        for m in req.history[-6:]:
            messages.append({"role": m.role, "content": m.text})
    messages.append({"role": "user", "content": req.message})

    # Haiku's non-English fluency is noticeably weaker than Sonnet's —
    # confirmed here by grammatically broken Burmese output. Burmese
    # replies specifically get the stronger model; English stays on Haiku
    # since that's where the cost/speed tradeoff is worth it.
    assistant_model = "claude-sonnet-5" if req.language == "mm" else "claude-haiku-4-5-20251001"

    reply_text = "Sorry, I couldn't process that right now. Try again in a moment."
    MAX_TOOL_ROUNDS = 5

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = claude_client.messages.create(
                model=assistant_model,
                max_tokens=500,
                system=system_prompt,
                tools=ASSISTANT_TOOLS,
                messages=messages,
            )

            if response.stop_reason != "tool_use":
                # Model produced a final text answer — done.
                reply_text = "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip() or reply_text
                break

            # Model wants to call one or more tools — execute each, append
            # the assistant's tool_use turn and our tool_result turn, then
            # loop so it can either call more tools or give a final answer.
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = _dispatch_assistant_tool(block.name, block.input, req, state)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            print("Assistant: hit MAX_TOOL_ROUNDS without a final answer")
    except Exception as e:
        print(f"Assistant Claude call failed: {e}")

    return AssistantMessageResponse(
        reply=reply_text,
        zone_context=state.get("zone_context"),
        report_submitted=state.get("report_submitted"),
    )


# ------------------------------------------------------------
# 11. Nearby Hospitals — real locations from OpenStreetMap (Overpass API)
#
# Free, no API key. Coverage depends on how well an area is mapped in OSM —
# generally solid for dense urban areas like Yangon, but not a guaranteed-
# complete hospital registry. Flag this honestly if asked; it's real data,
# not a fabricated list, but it's crowdsourced map data, not an official
# health-ministry registry.
# ------------------------------------------------------------
def _get_hospitals_google(lat: float, lng: float, radius_m: int) -> Optional[list[dict]]:
    """
    Real hospital/clinic locations via Google Places API (New) Nearby Search.
    Returns None (never raises) on any failure — including no API key set —
    so the caller can silently fall back to OSM/Overpass, same honest-
    fallback pattern used for routing (_get_route_google).

    Google Places generally has denser, more consistently maintained
    coverage than OSM outside central Yangon, but is a paid API past its
    free tier — that's the tradeoff for using it as primary here.
    """
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://places.googleapis.com/v1/places:searchNearby",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": (
                    "places.id,places.displayName,places.location,"
                    "places.internationalPhoneNumber,places.types"
                ),
            },
            json={
                "includedTypes": ["hospital"],
                "maxResultCount": 15,
                "locationRestriction": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lng},
                        "radius": float(radius_m),
                    }
                },
            },
            timeout=12,
        )
        resp.raise_for_status()
        places = resp.json().get("places", [])
    except Exception as e:
        print(f"Google Places hospital lookup failed, falling back to OSM: {e}")
        return None

    hospitals = []
    for p in places:
        loc = p.get("location", {})
        hlat, hlng = loc.get("latitude"), loc.get("longitude")
        if hlat is None or hlng is None:
            continue
        hospitals.append(
            {
                "id": f"google-{p.get('id')}",
                "name": (p.get("displayName") or {}).get("text", "Unnamed facility"),
                "lat": hlat,
                "lng": hlng,
                # Google Places doesn't expose a direct "24/7 emergency" flag
                # the way OSM's emergency=yes tag does — leaving this False
                # rather than guessing is the honest choice here.
                "emergency": False,
                "phone": p.get("internationalPhoneNumber"),
                "facility_type": "hospital",
                "source": "Google Places",
            }
        )
    return hospitals


# In-process cache for /hospitals/nearby — see comment inside the endpoint
# for why this exists. (lat_rounded, lng_rounded, radius_km) -> (cached_at_epoch, result_list)
_HOSPITAL_CACHE: dict[tuple[float, float, float], tuple[float, list[dict]]] = {}
HOSPITAL_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes — hospital locations don't move


@app.get("/hospitals/nearby")
def get_nearby_hospitals(
    lat: float = Query(...),
    lng: float = Query(...),
    radius_km: float = Query(15.0),
):
    # Simple in-process cache, keyed by lat/lng rounded to ~1km precision +
    # radius. Both hospital data sources (Google Places and Overpass) are
    # external services that can go down independently of your own app —
    # this means once ANY request for a given area succeeds, every other
    # visitor near that same spot for the next 30 minutes gets served
    # instantly from memory instead of also depending on Overpass/Google
    # being up at that exact moment. This resets on server restart (it's
    # in-process, not persisted) — fine for a hackathon demo; move to
    # Supabase/Redis if this needs to survive restarts later.
    cache_key = (round(lat, 2), round(lng, 2), round(radius_km, 1))
    cached = _HOSPITAL_CACHE.get(cache_key)
    if cached is not None:
        cached_at, cached_result = cached
        if datetime.utcnow().timestamp() - cached_at < HOSPITAL_CACHE_TTL_SECONDS:
            print(f"[DEBUG] hospitals cache hit for {cache_key}: {len(cached_result)} hospitals")
            return cached_result

    radius_m = int(radius_km * 1000)
    print(f"[DEBUG] GOOGLE_MAPS_API_KEY set: {bool(GOOGLE_MAPS_API_KEY)}")

    google_result = _get_hospitals_google(lat, lng, radius_m)
    print(f"[DEBUG] google_result: {None if google_result is None else len(google_result)} hospitals")
    if google_result is not None:
        def _dist_google(h: dict) -> float:
            return ((h["lat"] - lat) ** 2 + (h["lng"] - lng) ** 2) ** 0.5
        google_result.sort(key=_dist_google)
        capped = google_result[:15]
        _HOSPITAL_CACHE[cache_key] = (datetime.utcnow().timestamp(), capped)
        return capped

    # Broader tag coverage — OSM contributors in some regions tag hospitals
    # inconsistently (amenity=hospital, healthcare=hospital, or just
    # amenity=clinic for smaller facilities). Checking all three catches
    # more real facilities without inventing anything not in OSM.
    query = f"""
    [out:json][timeout:20];
    (
      node["amenity"="hospital"](around:{radius_m},{lat},{lng});
      way["amenity"="hospital"](around:{radius_m},{lat},{lng});
      node["healthcare"="hospital"](around:{radius_m},{lat},{lng});
      way["healthcare"="hospital"](around:{radius_m},{lat},{lng});
      node["amenity"="clinic"](around:{radius_m},{lat},{lng});
      way["amenity"="clinic"](around:{radius_m},{lat},{lng});
    );
    out center tags;
    """
    # Overpass has several independently-run public mirrors. overpass-api.de
    # (the default/main one) frequently 504s under load since it's the most
    # heavily used. Trying a short list of mirrors in order means one being
    # slow/down doesn't take out hospital data entirely.
    OVERPASS_MIRRORS = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.openstreetmap.ru/api/interpreter",
    ]
    headers = {
        # Overpass's public instances return 406 Not Acceptable for requests
        # carrying a generic/default User-Agent (e.g. plain
        # "python-requests/x.y") — they expect a real, identifying client
        # string. Also explicitly ask for JSON.
        "User-Agent": "UrbanHeatIntelligencePlatform/1.0 (hackathon project)",
        "Accept": "application/json",
    }

    elements = []
    last_error = None
    for mirror_url in OVERPASS_MIRRORS:
        try:
            # Reduced from 12s to 5s per mirror. These are free, shared,
            # increasingly unreliable public mirrors — waiting up to 36s
            # total (3 mirrors x 12s) before giving up is a bad experience
            # for a real visitor even when it eventually succeeds. Failing
            # faster means the map/fallback state shows up sooner; a
            # genuinely slow-but-working mirror gets skipped in favor of a
            # faster one, which is the right tradeoff here.
            resp = requests.post(mirror_url, data={"data": query}, timeout=5, headers=headers)
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
            last_error = None
            break  # got a good response, stop trying further mirrors
        except Exception as e:
            last_error = e
            print(f"Hospital lookup failed on {mirror_url}: {e}")
            continue  # try the next mirror

    if last_error is not None:
        # All mirrors failed — Overpass is a free, shared, occasionally
        # slow/rate-limited public service. Don't fail the whole page over
        # it; log and return an empty list so the map just shows no
        # hospital pins instead of an error the user can't do anything about.
        print(f"Hospital lookup failed on all Overpass mirrors: {last_error}")
        return []

    hospitals = []
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "node":
            hlat, hlng = el.get("lat"), el.get("lon")
        else:
            center = el.get("center", {})
            hlat, hlng = center.get("lat"), center.get("lon")
        if hlat is None or hlng is None:
            continue
        hospitals.append(
            {
                "id": f"osm-{el['type']}-{el['id']}",
                "name": tags.get("name", "Unnamed facility"),
                "lat": hlat,
                "lng": hlng,
                "emergency": tags.get("emergency") == "yes",
                "phone": tags.get("phone") or tags.get("contact:phone"),
                "facility_type": tags.get("amenity") or tags.get("healthcare") or "hospital",
                "source": "OpenStreetMap",
            }
        )
    # Cap to the nearest 15, sorted by straight-line distance from the query
    # point. Applied regardless of source: Google's maxResultCount already
    # limits it to 20, but the Overpass fallback has no such cap and can
    # return hundreds of clinics/hospitals within a 15km radius, which is
    # both overkill for the map and slow to render.
    def _dist(h: dict) -> float:
        return ((h["lat"] - lat) ** 2 + (h["lng"] - lng) ** 2) ** 0.5

    hospitals.sort(key=_dist)
    capped = hospitals[:15]
    _HOSPITAL_CACHE[cache_key] = (datetime.utcnow().timestamp(), capped)
    return capped


# ------------------------------------------------------------
# 12. Route Directions — real road-following route via OSRM (free, no key)
#
# HONEST SCOPE NOTE: OSRM's public demo server (router.project-osrm.org) has
# no live traffic awareness — ETAs assume free-flow speeds, not current
# conditions. It's also a shared public demo server, not something with an
# uptime guarantee for production. Real routing geometry/ETA, just not
# traffic-aware. This is a genuine upgrade over the old straight-line
# heuristic in /route/safety-check, not a replacement for it — combine both
# if you want a route that's both real-road-following AND heat-risk-aware.
# ------------------------------------------------------------
class RouteDirectionsRequest(BaseModel):
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float


def _decode_polyline(encoded: str) -> list[list[float]]:
    """Decode Google's encoded polyline format into [lng, lat] pairs, matching
    the GeoJSON coordinate order the frontend already expects from OSRM."""
    points = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lng += delta
        points.append([lng / 1e5, lat / 1e5])
    return points


def _get_route_google(req: RouteDirectionsRequest) -> Optional[dict]:
    """Real, live traffic-aware routing via Google's Routes API. Returns None
    (never raises) on any failure so the caller can silently fall back to
    OSRM — same honest-fallback pattern used throughout this file."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": (
                    "routes.duration,routes.distanceMeters,"
                    "routes.polyline.encodedPolyline,routes.staticDuration"
                ),
            },
            json={
                "origin": {"location": {"latLng": {"latitude": req.origin_lat, "longitude": req.origin_lng}}},
                "destination": {"location": {"latLng": {"latitude": req.dest_lat, "longitude": req.dest_lng}}},
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        route = data["routes"][0]
        duration_s = float(route["duration"].rstrip("s"))
        static_duration_s = float(route.get("staticDuration", route["duration"]).rstrip("s"))
        coords = _decode_polyline(route["polyline"]["encodedPolyline"])
        return {
            "distance_m": route["distanceMeters"],
            "duration_s": duration_s,
            "duration_no_traffic_s": static_duration_s,
            "geometry": {"type": "LineString", "coordinates": coords},
            "note": "Live traffic-aware route via Google Routes API.",
            "provider": "google",
        }
    except Exception as e:
        print(f"Google Routes API failed, falling back to OSRM: {e}")
        return None


@app.post("/route/directions")
def get_route_directions(req: RouteDirectionsRequest):
    google_result = _get_route_google(req)
    if google_result:
        return google_result

    # --- Fallback: OSRM, free, no live traffic ---
    url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{req.origin_lng},{req.origin_lat};{req.dest_lng},{req.dest_lat}"
        f"?overview=full&geometries=geojson"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Routing failed: {e}")

    if data.get("code") != "Ok" or not data.get("routes"):
        raise HTTPException(status_code=404, detail="No route found")

    route = data["routes"][0]
    return {
        "distance_m": route["distance"],
        "duration_s": route["duration"],
        "duration_no_traffic_s": None,
        "geometry": route["geometry"],
        "note": "Free-flow ETA, no live traffic data — OSRM public demo server.",
        "provider": "osrm",
    }


# ------------------------------------------------------------
# Admin: externally-triggered weather refresh
#
# On Render's free tier, the process spins down after ~15 min of no
# traffic and cold-starts on the next request. The in-process
# BackgroundScheduler above only runs while the process is alive, so it
# can't reliably deliver "every 10 minutes" on its own — it depends on
# something else keeping the process awake or waking it back up.
#
# This endpoint lets an EXTERNAL scheduler (e.g. a free cron service like
# cron-job.org, or Render's own separate Cron Job service type) trigger a
# refresh by hitting this URL every 10 minutes. Every hit is itself
# incoming traffic, which also resets Render's 15-minute idle timer — so
# one external cron job does double duty: keeps the app awake AND
# triggers the actual refresh, without depending on the in-process
# scheduler at all. The in-process scheduler above is left in place as a
# harmless belt-and-suspenders extra (it'll also fire while awake), but
# this endpoint is the reliable mechanism on free tier.
#
# Protected by a shared secret so random internet traffic can't spam your
# OpenWeatherMap free-tier quota. Set WEATHER_REFRESH_SECRET in your
# Render environment variables to any random string, then configure your
# external cron to call:
#   POST https://your-app.onrender.com/admin/refresh-weather?secret=YOUR_SECRET
# ------------------------------------------------------------
WEATHER_REFRESH_SECRET = os.environ.get("WEATHER_REFRESH_SECRET")


@app.post("/admin/refresh-weather")
def admin_refresh_weather(secret: str = Query(...)):
    if not WEATHER_REFRESH_SECRET:
        raise HTTPException(
            status_code=503,
            detail="WEATHER_REFRESH_SECRET is not set on the server — refusing to run an unprotected admin endpoint.",
        )
    if secret != WEATHER_REFRESH_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        update_weather.main()
        return {"status": "ok", "refreshed_at": datetime.utcnow().isoformat()}
    except Exception as e:
        print(f"[admin] Manual weather refresh failed: {e}")
        raise HTTPException(status_code=500, detail=f"Refresh failed: {e}")


# ------------------------------------------------------------
# Health check
# ------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}