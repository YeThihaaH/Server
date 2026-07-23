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
    scheduler.add_job(_run_weather_refresh, "interval", minutes=10, next_run_time=None)
    scheduler.start()
    print("[scheduler] Live weather refresh scheduled every 10 minutes.")


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
# 10. Heat Safety Assistant — conversational, grounded in real location data
#
# HONEST SCOPE NOTE: this finds the user's nearest zone by straight-line
# distance to zone centroids (same approach as the route safety-check
# above), not a precise polygon lookup — reasonable for a city-scale zone
# grid, not perfectly precise at a zone boundary. Every reply is grounded
# in that zone's real current data plus real nearby cooling centers, not
# free-floating generation.
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


class AssistantMessageResponse(BaseModel):
    reply: str
    zone_context: Optional[dict] = None


def _find_nearest_zone(lat: float, lng: float, zones: list[dict]) -> Optional[dict]:
    best, best_dist = None, float("inf")
    for z in zones:
        d = ((z["centroid_lat"] - lat) ** 2 + (z["centroid_lng"] - lng) ** 2) ** 0.5
        if d < best_dist:
            best, best_dist = z, d
    return best


@app.post("/assistant/message", response_model=AssistantMessageResponse)
def assistant_message(req: AssistantMessageRequest):
    zone_context = None
    nearby_centers: list[dict] = []

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
        try:
            centers_result = supabase.rpc(
                "nearby_cooling_centers",
                {"lat": req.lat, "lng": req.lng, "radius_km": 5.0},
            ).execute()
            nearby_centers = (centers_result.data or [])[:3]
        except Exception as e:
            print(f"Assistant: nearby cooling centers lookup failed: {e}")

    if claude_client is None:
        # Fallback: real data, templated text, no LLM — English only, since
        # translating this fallback would need either a translation library
        # or hardcoded Burmese strings, neither of which exists yet. Flag
        # this honestly rather than serving broken/mixed-language text.
        if zone_context:
            reply = (
                f"You're near {zone_context['name']}, currently {zone_context['risk_level']} risk "
                f"at {zone_context['current_temp_c']}°C. "
            )
            if nearby_centers:
                reply += f"Nearest cooling option: {nearby_centers[0].get('name', 'a nearby center')}."
            else:
                reply += "No nearby cooling centers found in range."
        else:
            reply = "Share your location so I can give a recommendation grounded in your nearest zone's real data."
        return AssistantMessageResponse(reply=reply, zone_context=zone_context)

    context_block = ""
    if zone_context:
        context_block += (
            f"\nUser's nearest monitored zone: {zone_context['name']}, "
            f"{zone_context['risk_level']} risk, {zone_context['current_temp_c']}°C, "
            f"{zone_context['green_cover_pct']}% green cover."
        )
    if nearby_centers:
        names = ", ".join(c.get("name", "unnamed") for c in nearby_centers)
        context_block += f"\nNearby cooling centers/water stations: {names}."
    if not zone_context and not nearby_centers:
        context_block = "\nNo location shared yet — don't assume a location, ask for it if relevant."

    history_block = ""
    if req.history:
        history_block = "\n".join(f"{m.role}: {m.text}" for m in req.history[-6:])

    language_instruction = (
        "Respond in Burmese (Myanmar language), using natural everyday Burmese, not literal word-for-word translation."
        if req.language == "mm"
        else "Respond in English."
    )

    prompt = f"""You are a heat-safety assistant for a citizen-facing urban heat app. Be concise (2-4 sentences),
practical, and only state facts you're given below — never invent a specific zone name, temperature,
or cooling-center name that isn't in this context. {language_instruction}
{context_block}

Recent conversation:
{history_block}

User's new message: {req.message}

Reply directly to the user, in plain conversational text, no preamble."""

    try:
        response = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        reply = response.content[0].text.strip()
    except Exception as e:
        print(f"Assistant Claude call failed: {e}")
        reply = "Sorry, I couldn't process that right now. Try again in a moment."

    return AssistantMessageResponse(reply=reply, zone_context=zone_context)


# ------------------------------------------------------------
# 11. Nearby Hospitals — real locations from OpenStreetMap (Overpass API)
#
# Free, no API key. Coverage depends on how well an area is mapped in OSM —
# generally solid for dense urban areas like Yangon, but not a guaranteed-
# complete hospital registry. Flag this honestly if asked; it's real data,
# not a fabricated list, but it's crowdsourced map data, not an official
# health-ministry registry.
# ------------------------------------------------------------
@app.get("/hospitals/nearby")
def get_nearby_hospitals(
    lat: float = Query(...),
    lng: float = Query(...),
    radius_km: float = Query(15.0),
):
    radius_m = int(radius_km * 1000)
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
    try:
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=12,
        )
        resp.raise_for_status()
        elements = resp.json().get("elements", [])
    except Exception as e:
        # Overpass is a free, shared, occasionally slow/rate-limited public
        # service — don't fail the whole page over it. Log and return an
        # empty list so the map just shows no hospital pins instead of an
        # error the user can't do anything about.
        print(f"Hospital lookup failed (Overpass): {e}")
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
    return hospitals


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
# Health check
# ------------------------------------------------------------
@app.get("/health")

def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}