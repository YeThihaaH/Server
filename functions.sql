-- ============================================================
-- RPC Functions — run AFTER schema.sql
-- Supabase's REST layer can't do PostGIS spatial queries directly,
-- so these functions are called via supabase.rpc(...) from FastAPI.
-- ============================================================

-- 1. Find cooling centers within X km of a lat/lng point
create or replace function nearby_cooling_centers(lat float, lng float, radius_km float default 5.0)
returns table (
  id uuid,
  name text,
  distance_km float,
  capacity integer,
  is_active boolean,
  hours text,
  contact text
) as $$
  select
    c.id,
    c.name,
    st_distance(c.geom::geography, st_setsrid(st_makepoint(lng, lat), 4326)::geography) / 1000 as distance_km,
    c.capacity,
    c.is_active,
    c.hours,
    c.contact
  from cooling_centers c
  where c.is_active = true
    and st_dwithin(
      c.geom::geography,
      st_setsrid(st_makepoint(lng, lat), 4326)::geography,
      radius_km * 1000
    )
  order by distance_km asc;
$$ language sql stable;

-- 2. Insert a citizen heat report from lat/lng, auto-detect which zone it falls in
create or replace function insert_heat_report(
  p_lat float,
  p_lng float,
  p_description text default null,
  p_reported_temp numeric default null,
  p_zone_id uuid default null
)
returns heat_reports as $$
declare
  detected_zone_id uuid;
  new_report heat_reports;
begin
  -- if zone_id not provided, find which zone polygon contains this point
  if p_zone_id is null then
    select id into detected_zone_id
    from heat_zones
    where st_contains(geom, st_setsrid(st_makepoint(p_lng, p_lat), 4326))
    limit 1;
  else
    detected_zone_id := p_zone_id;
  end if;

  insert into heat_reports (zone_id, geom, description, reported_temp)
  values (
    detected_zone_id,
    st_setsrid(st_makepoint(p_lng, p_lat), 4326),
    p_description,
    p_reported_temp
  )
  returning * into new_report;

  return new_report;
end;
$$ language plpgsql;

-- 3. Recompute a zone's risk_level based on latest reading (call after ingestion)
create or replace function recompute_zone_risk(p_zone_id uuid)
returns void as $$
declare
  latest_temp numeric;
begin
  select temperature into latest_temp
  from heat_readings
  where zone_id = p_zone_id
  order by recorded_at desc
  limit 1;

  update heat_zones
  set
    current_temp = latest_temp,
    risk_level = case
      when latest_temp >= 36 then 'high'
      when latest_temp >= 32 then 'medium'
      else 'low'
    end,
    last_updated = now()
  where id = p_zone_id;
end;
$$ language plpgsql;
