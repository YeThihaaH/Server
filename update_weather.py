"""
Weather Update Script — pulls real current temperature for each zone
from OpenWeatherMap and writes it into heat_readings, then recomputes risk_level.

Run manually before your demo:
    python update_weather.py

Or schedule it (e.g. every 15-30 min) with a simple loop / cron / n8n later.
"""

import os
import time
import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env")
if not OPENWEATHER_API_KEY:
    raise RuntimeError(
        "Set OPENWEATHER_API_KEY in .env. Get a free key at https://openweathermap.org/api"
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"


def get_zone_centroids():
    """
    Pull each zone's centroid lat/lng directly via a small RPC,
    since Supabase's REST layer can't read PostGIS geometry columns natively.
    """
    result = supabase.rpc("get_zone_centroids").execute()
    return result.data


def fetch_temperature(lat: float, lng: float) -> float:
    """Call OpenWeatherMap current weather API, return temp in Celsius."""
    resp = requests.get(
        WEATHER_URL,
        params={
            "lat": lat,
            "lon": lng,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return round(data["main"]["temp"], 1)


def update_zone(zone_id: str, temperature: float):
    """Insert a new reading, then recompute the zone's risk_level."""
    supabase.table("heat_readings").insert(
        {
            "zone_id": zone_id,
            "temperature": temperature,
            "source": "weather_api",
        }
    ).execute()

    supabase.rpc("recompute_zone_risk", {"p_zone_id": zone_id}).execute()


def main():
    zones = get_zone_centroids()
    print(f"Found {len(zones)} zones. Fetching live weather...\n")

    for zone in zones:
        zone_id = zone["id"]
        name = zone["zone_name"]
        lat = zone["lat"]
        lng = zone["lng"]

        try:
            temp = fetch_temperature(lat, lng)
            update_zone(zone_id, temp)
            print(f"✅ {name}: {temp}°C")
        except Exception as e:
            print(f"❌ {name}: failed — {e}")

        time.sleep(1)  # stay well under free-tier rate limits

    print("\nDone. Check /heat-zones to see updated live temperatures.")


if __name__ == "__main__":
    main()
