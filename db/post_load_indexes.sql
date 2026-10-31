-- Run by scripts/load_gtfs.py after COPY. Each index matches a query shape
-- the text-to-SQL path will generate often.

-- "What leaves stop X ..." : stop -> stop_times -> trips
CREATE INDEX IF NOT EXISTS stop_times_stop_dep_idx ON gtfs.stop_times (stop_id, departure_secs);
-- "Which trips does route R run" : route -> trips
CREATE INDEX IF NOT EXISTS trips_route_idx ON gtfs.trips (route_id);
CREATE INDEX IF NOT EXISTS trips_service_idx ON gtfs.trips (service_id);
-- Station -> its platforms and stands
CREATE INDEX IF NOT EXISTS stops_parent_idx ON gtfs.stops (parent_station);
-- Users name routes by short name ("T4", "M20")
CREATE INDEX IF NOT EXISTS routes_short_name_idx ON gtfs.routes (lower(route_short_name));
-- Fuzzy stop-name resolution: "hurstvile stn" -> Hurstville Station
CREATE INDEX IF NOT EXISTS stops_name_trgm_idx ON gtfs.stops USING gin (stop_name gin_trgm_ops);
-- Holiday and trackwork exceptions by date
CREATE INDEX IF NOT EXISTS calendar_dates_date_idx ON gtfs.calendar_dates (date);
