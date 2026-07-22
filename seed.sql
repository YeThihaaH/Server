-- ============================================================
-- Seed Data — Yangon-area zones (swap coords for your target city)
-- Run AFTER schema.sql
-- Gives frontend teammates real-looking data from hour one.
-- ============================================================

-- Sample zones (rough bounding boxes around real Yangon townships)
insert into heat_zones (zone_name, geom, current_temp, risk_level, population_density, green_cover_pct)
values
  ('Downtown Yangon',
   st_geomfromtext('POLYGON((96.155 16.775, 96.175 16.775, 96.175 16.790, 96.155 16.790, 96.155 16.775))', 4326),
   38.5, 'high', 28000, 5.2),

  ('Kamayut Township',
   st_geomfromtext('POLYGON((96.115 16.845, 96.135 16.845, 96.135 16.860, 96.115 16.860, 96.115 16.845))', 4326),
   34.2, 'medium', 15000, 18.7),

  ('Insein Township',
   st_geomfromtext('POLYGON((96.085 16.895, 96.110 16.895, 96.110 16.915, 96.085 16.915, 96.085 16.895))', 4326),
   32.8, 'medium', 12000, 22.3),

  ('Hlaing Township',
   st_geomfromtext('POLYGON((96.115 16.815, 96.140 16.815, 96.140 16.835, 96.115 16.835, 96.115 16.815))', 4326),
   36.1, 'high', 21000, 9.8),

  ('Thingangyun Township',
   st_geomfromtext('POLYGON((96.185 16.815, 96.210 16.815, 96.210 16.835, 96.185 16.835, 96.185 16.815))', 4326),
   33.4, 'medium', 17000, 15.1),

  ('Dagon Seikkan (green belt area)',
   st_geomfromtext('POLYGON((96.230 16.860, 96.260 16.860, 96.260 16.885, 96.230 16.885, 96.230 16.860))', 4326),
   29.6, 'low', 6000, 41.0),

  ('Mingalar Taung Nyunt',
   st_geomfromtext('POLYGON((96.165 16.780, 96.185 16.780, 96.185 16.795, 96.165 16.795, 96.165 16.780))', 4326),
   37.9, 'high', 26000, 6.4),

  ('Bahan Township (near lakes/park)',
   st_geomfromtext('POLYGON((96.150 16.800, 96.170 16.800, 96.170 16.815, 96.150 16.815, 96.150 16.800))', 4326),
   30.9, 'low', 14000, 33.5);

-- Sample cooling centers
insert into cooling_centers (name, geom, capacity, is_active, hours, contact)
values
  ('People''s Square Cooling Hub', st_geomfromtext('POINT(96.161 16.789)', 4326), 200, true, '8am - 8pm', '+95-1-234567'),
  ('Kamayut Community Center', st_geomfromtext('POINT(96.125 16.851)', 4326), 80, true, '9am - 6pm', '+95-1-234568'),
  ('Insein Public Library', st_geomfromtext('POINT(96.097 16.905)', 4326), 60, true, '8am - 5pm', '+95-1-234569'),
  ('Thingangyun Youth Hall', st_geomfromtext('POINT(96.197 16.825)', 4326), 100, true, '24 hours', '+95-1-234570');

-- Sample historical readings (a few days back, for trend charts)
insert into heat_readings (zone_id, temperature, source, recorded_at)
select id, current_temp - (random() * 2), 'weather_api', now() - interval '1 day'
from heat_zones;

insert into heat_readings (zone_id, temperature, source, recorded_at)
select id, current_temp - (random() * 3), 'weather_api', now() - interval '2 days'
from heat_zones;

insert into heat_readings (zone_id, temperature, source, recorded_at)
select id, current_temp, 'satellite', now()
from heat_zones;

-- Sample intervention proposals
insert into interventions (zone_id, type, quantity, estimated_impact_c, confidence, status)
select id, 'tree_planting', 150, 1.8, 0.72, 'proposed'
from heat_zones where risk_level = 'high'
limit 2;
