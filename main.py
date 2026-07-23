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
from apscheduler.schedulers.background import BackgroundScheduler

import update_weather  # reuses the exact same logic as the manual script

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
# Health check
# ------------------------------------------------------------
@app.get("/health")

def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}