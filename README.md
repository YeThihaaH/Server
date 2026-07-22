# Urban Heat Intelligence — Backend

## Setup order

1. **Create Supabase project** at supabase.com, note your Project URL and `service_role` key (Settings → API).

2. **Run SQL in this order** (Supabase Dashboard → SQL Editor):
   1. `schema.sql` — creates tables, indexes, RLS policies
   2. `functions.sql` — creates PostGIS RPC functions (nearby search, report insert, risk recompute)
   3. `seed.sql` — seeds sample Yangon zones + cooling centers so frontend devs have real data immediately

3. **Configure environment**
   ```bash
   cp .env.example .env
   # then edit .env with your actual SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
   ```

4. **Install dependencies**
   ```bash
   python -m venv venv
   source venv/bin/activate        # or venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

5. **Load env vars and run**
   ```bash
   export $(cat .env | xargs)      # or use python-dotenv / your IDE's env support
   uvicorn main:app --reload --port 8000
   ```

6. **Test it**
   - `GET http://localhost:8000/health`
   - `GET http://localhost:8000/heat-zones`
   - `GET http://localhost:8000/cooling-centers/nearby?lat=16.79&lng=96.16&radius_km=5`
   - `GET http://localhost:8000/dashboard/rankings`

## Endpoint reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/heat-zones` | All zones + current risk level (map layer) |
| GET | `/heat-zones/{id}` | Zone detail + historical readings |
| GET | `/cooling-centers/nearby` | Nearest cooling centers by lat/lng |
| GET | `/dashboard/rankings` | Zones sorted by risk (gov dashboard) |
| POST | `/interventions/estimate` | Predict temp reduction for a proposed intervention |
| POST | `/interventions` | Save a proposed intervention |
| GET | `/interventions/{zone_id}` | List interventions for a zone |
| POST | `/reports` | Citizen submits a heat report at a location |
| GET | `/health` | Health check |

## Integration contracts to lock down with teammates

- **Data Engineer (ingestion):** their pipeline should insert rows into `heat_readings`
  (`zone_id, temperature, source, recorded_at`), then call `select recompute_zone_risk('<zone_id>')`
  to refresh `heat_zones.risk_level`. Wire this as an n8n workflow on a schedule.

- **AI Engineer (intervention estimator):** replace the placeholder heuristic inside
  `estimate_intervention_impact()` in `main.py` with a real call to their model/endpoint.
  Keep the request/response shape (`InterventionEstimateRequest` / `Response`) as the agreed contract
  so frontend work doesn't break when you swap it in.

## Notes

- Uses the Supabase **service_role** key so the backend can bypass RLS for writes — never expose
  this key to either frontend app. Frontends should only ever call your FastAPI endpoints, not
  Supabase directly with this key.
- CORS is wide open (`*`) for hackathon speed — fine for a demo, not for production.
- Swap the Yangon seed coordinates in `seed.sql` if you're demoing a different city.
