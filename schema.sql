-- ============================================================
-- AI Urban Heat Intelligence Platform — Database Schema
-- Run this in Supabase SQL Editor (or psql) after enabling PostGIS
-- ============================================================

-- 1. Enable PostGIS extension
create extension if not exists postgis;

-- ============================================================
-- 2. Heat Zones (neighborhoods / grid cells)
-- ============================================================
create table if not exists heat_zones (
  id uuid primary key default gen_random_uuid(),
  zone_name text not null,
  geom geometry(Polygon, 4326) not null,       -- neighborhood boundary
  centroid geometry(Point, 4326),               -- for quick distance queries
  current_temp numeric(5,2),                    -- in Celsius
  risk_level text check (risk_level in ('low', 'medium', 'high')) default 'low',
  population_density integer,                   -- optional, helps prioritize
  green_cover_pct numeric(5,2),                 -- % tree/vegetation cover
  last_updated timestamptz default now()
);

create index if not exists idx_heat_zones_geom on heat_zones using gist (geom);
create index if not exists idx_heat_zones_centroid on heat_zones using gist (centroid);
create index if not exists idx_heat_zones_risk on heat_zones (risk_level);

-- ============================================================
-- 3. Heat Readings (time-series, for trends + prediction)
-- ============================================================
create table if not exists heat_readings (
  id uuid primary key default gen_random_uuid(),
  zone_id uuid references heat_zones(id) on delete cascade,
  temperature numeric(5,2) not null,
  source text check (source in ('satellite', 'weather_api', 'sensor', 'manual')) default 'weather_api',
  recorded_at timestamptz default now()
);

create index if not exists idx_heat_readings_zone on heat_readings (zone_id, recorded_at desc);

-- ============================================================
-- 4. Cooling Centers
-- ============================================================
create table if not exists cooling_centers (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  geom geometry(Point, 4326) not null,
  capacity integer,
  is_active boolean default true,
  hours text,                                   -- e.g. "8am - 8pm"
  contact text,
  created_at timestamptz default now()
);

create index if not exists idx_cooling_centers_geom on cooling_centers using gist (geom);

-- ============================================================
-- 5. Interventions (AI-recommended actions + impact estimates)
-- ============================================================
create table if not exists interventions (
  id uuid primary key default gen_random_uuid(),
  zone_id uuid references heat_zones(id) on delete cascade,
  type text check (type in ('tree_planting', 'cooling_center', 'material_change', 'shade_structure')) not null,
  quantity numeric,                             -- e.g. number of trees, sq meters
  estimated_impact_c numeric(4,2),              -- predicted temp reduction
  confidence numeric(4,3),                      -- model confidence 0-1
  status text check (status in ('proposed', 'approved', 'in_progress', 'completed')) default 'proposed',
  proposed_at timestamptz default now()
);

create index if not exists idx_interventions_zone on interventions (zone_id);

-- ============================================================
-- 6. Citizen Reports (optional — if citizens can flag extreme heat spots)
-- ============================================================
create table if not exists heat_reports (
  id uuid primary key default gen_random_uuid(),
  zone_id uuid references heat_zones(id) on delete set null,
  geom geometry(Point, 4326) not null,
  description text,
  reported_temp numeric(5,2),
  created_at timestamptz default now()
);

create index if not exists idx_heat_reports_geom on heat_reports using gist (geom);

-- ============================================================
-- 7. Row Level Security (public read, restricted write)
-- ============================================================
alter table heat_zones enable row level security;
alter table heat_readings enable row level security;
alter table cooling_centers enable row level security;
alter table interventions enable row level security;
alter table heat_reports enable row level security;

-- Public read access (fine for hackathon demo — tighten later)
create policy "public read heat_zones" on heat_zones for select using (true);
create policy "public read heat_readings" on heat_readings for select using (true);
create policy "public read cooling_centers" on cooling_centers for select using (true);
create policy "public read interventions" on interventions for select using (true);
create policy "public read heat_reports" on heat_reports for select using (true);

-- Writes restricted to service_role only (your backend uses service_role key)
create policy "service write heat_zones" on heat_zones for all using (auth.role() = 'service_role');
create policy "service write heat_readings" on heat_readings for all using (auth.role() = 'service_role');
create policy "service write cooling_centers" on cooling_centers for all using (auth.role() = 'service_role');
create policy "service write interventions" on interventions for all using (auth.role() = 'service_role');

-- Citizens can insert their own reports (adjust if you add auth)
create policy "anyone can insert heat_reports" on heat_reports for insert with check (true);

-- ============================================================
-- 8. Helper function: auto-update centroid when geom changes
-- ============================================================
create or replace function set_zone_centroid()
returns trigger as $$
begin
  new.centroid := st_centroid(new.geom);
  return new;
end;
$$ language plpgsql;

create trigger trg_set_zone_centroid
before insert or update of geom on heat_zones
for each row execute function set_zone_centroid();
