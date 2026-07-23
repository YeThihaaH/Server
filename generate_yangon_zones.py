"""
Generate real Yangon township zone data from OpenStreetMap's Nominatim API.

This expands your 8 seeded zones to all 33 official Yangon Region townships,
using REAL bounding boxes from OpenStreetMap (not guessed coordinates).

Run with: python generate_yangon_zones.py > yangon_zones_seed.sql
Then paste the generated SQL into Supabase SQL Editor and run it.

Respects Nominatim's usage policy: max 1 request/second, requires a
descriptive User-Agent. https://operations.osmfoundation.org/policies/nominatim/
"""

import time
import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "UrbanHeatIntelligencePlatform-Hackathon/1.0 (contact: your-email@example.com)"}

# All 33 official townships of Yangon Region
YANGON_TOWNSHIPS = [
    "Ahlone", "Bahan", "Botahtaung", "Dagon", "Dagon Seikkan", "Dala",
    "Dawbon", "East Dagon", "Hlaingtharya", "Hlaing", "Insein", "Kamaryut",
    "Kawhmu", "Kayan", "Kungyangon", "Kyauktada", "Kyauktan", "Kyeemyindaing",
    "Lanmadaw", "Latha", "Mayangone", "Mingala Taungnyunt", "Mingaladon",
    "North Dagon", "North Okkalapa", "Pabedan", "Pazundaung", "Sanchaung",
    "Seikkyi Khanaungto", "Shwepyithar", "South Dagon", "South Okkalapa",
    "Tamwe", "Thaketa", "Thanlyin", "Thingangyun", "Twantay", "Yankin",
]


def fetch_township_bbox(name: str):
    """Query Nominatim for a township's bounding box within Yangon, Myanmar."""
    resp = requests.get(
        NOMINATIM_URL,
        params={
            "q": f"{name} Township, Yangon, Myanmar",
            "format": "json",
            "limit": 1,
        },
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    r = results[0]
    # Nominatim returns boundingbox as [south, north, west, east] (strings)
    south, north, west, east = map(float, r["boundingbox"])
    return {"south": south, "north": north, "west": west, "east": east}


def main():
    print("-- ============================================================")
    print("-- Auto-generated Yangon township zones from OpenStreetMap Nominatim")
    print("-- Boundaries are approximate bounding boxes, not precise polygons —")
    print("-- good enough for zone-level heat risk display, not survey-grade GIS.")
    print("-- ============================================================\n")

    for name in YANGON_TOWNSHIPS:
        bbox = fetch_township_bbox(name)
        time.sleep(1.1)  # respect Nominatim's 1 req/sec rate limit

        if not bbox:
            print(f"-- SKIPPED: no result found for '{name}'")
            continue

        zone_name = f"{name} Township"
        print(
            f"""insert into heat_zones (zone_name, geom, current_temp, risk_level, population_density, green_cover_pct)
values (
  '{zone_name}',
  st_geomfromtext('POLYGON(({bbox['west']} {bbox['south']}, {bbox['east']} {bbox['south']}, {bbox['east']} {bbox['north']}, {bbox['west']} {bbox['north']}, {bbox['west']} {bbox['south']}))', 4326),
  32.0, 'medium', 15000, 15.0
)
on conflict do nothing;
"""
        )
        print(f"-- fetched: {zone_name}", flush=True)

    print("\n-- Done. Run update_weather.py afterward to replace the placeholder")
    print("-- current_temp/risk_level values above with real live weather data.")


if __name__ == "__main__":
    main()
