-- GTFS static schema for the TfNSW "Timetables Complete GTFS" feed.
--
-- Design notes
-- * No foreign keys: the full feed is bulk-loaded with COPY and real feeds
--   contain the odd orphan row. Integrity is checked after load instead
--   (see scripts/load_gtfs.py).
-- * Secondary indexes are created after loading (db/post_load_indexes.sql),
--   which is much faster than maintaining them during COPY.
-- * GTFS times can exceed 24:00:00 (a trip that starts on Monday's service
--   day and runs past midnight). They are stored as text exactly as published,
--   plus an integer seconds-after-midnight column for sorting and arithmetic.
-- * COMMENTs are deliberate: they are pulled into the text-to-SQL prompt as
--   the schema description, so write them for the model as much as for you.

CREATE TABLE IF NOT EXISTS gtfs.agency (
    agency_id        text PRIMARY KEY,
    agency_name      text NOT NULL,
    agency_url       text,
    agency_timezone  text,
    agency_lang      text,
    agency_phone     text
);
COMMENT ON TABLE gtfs.agency IS 'Transport operators, e.g. Sydney Trains and individual bus operators.';

CREATE TABLE IF NOT EXISTS gtfs.stops (
    stop_id              text PRIMARY KEY,
    stop_code            text,
    stop_name            text,
    stop_lat             double precision,
    stop_lon             double precision,
    location_type        smallint,
    parent_station       text,
    wheelchair_boarding  smallint,
    platform_code        text
);
COMMENT ON TABLE gtfs.stops IS 'Stops, platforms, bus stands and stations. A station (location_type = 1) groups its platforms and stands through parent_station.';
COMMENT ON COLUMN gtfs.stops.location_type IS '0 or NULL = stop/platform where vehicles stop, 1 = station (parent), 2 = entrance/exit, 3 = generic node, 4 = boarding area.';
COMMENT ON COLUMN gtfs.stops.parent_station IS 'stop_id of the parent station for platforms and stands. To find everything at a station, match the station row and all rows whose parent_station is that station.';
COMMENT ON COLUMN gtfs.stops.wheelchair_boarding IS '0 or NULL = no information, 1 = some wheelchair access possible, 2 = not wheelchair accessible.';

CREATE TABLE IF NOT EXISTS gtfs.routes (
    route_id          text PRIMARY KEY,
    agency_id         text,
    route_short_name  text,
    route_long_name   text,
    route_desc        text,
    route_type        integer,
    route_color       text,
    route_text_color  text
);
COMMENT ON TABLE gtfs.routes IS 'A route is a line customers recognise, e.g. the T4 train line or a numbered bus route.';
COMMENT ON COLUMN gtfs.routes.route_short_name IS 'Public-facing route number or line code, e.g. T4. Match on this when the user names a route.';
COMMENT ON COLUMN gtfs.routes.route_type IS 'Mode of transport. TfNSW uses both basic GTFS codes and extended codes (the 700 range is bus). Run SELECT route_type, COUNT(*) FROM gtfs.routes GROUP BY 1 to see the codes in your feed.';

CREATE TABLE IF NOT EXISTS gtfs.trips (
    trip_id                text PRIMARY KEY,
    route_id               text NOT NULL,
    service_id             text NOT NULL,
    trip_headsign          text,
    trip_short_name        text,
    direction_id           smallint,
    block_id               text,
    shape_id               text,
    wheelchair_accessible  smallint
);
COMMENT ON TABLE gtfs.trips IS 'One scheduled journey of a vehicle along a route. service_id links to calendar/calendar_dates to say which days it runs.';
COMMENT ON COLUMN gtfs.trips.direction_id IS '0 or 1. Distinguishes the two directions of a route; the meaning is route-specific, so use trip_headsign to describe direction to users.';

CREATE TABLE IF NOT EXISTS gtfs.stop_times (
    trip_id              text NOT NULL,
    arrival_time         text,
    departure_time       text,
    arrival_secs         integer,
    departure_secs       integer,
    stop_id              text NOT NULL,
    stop_sequence        integer NOT NULL,
    stop_headsign        text,
    pickup_type          smallint,
    drop_off_type        smallint,
    shape_dist_traveled  double precision,
    timepoint            smallint,
    PRIMARY KEY (trip_id, stop_sequence)
);
COMMENT ON TABLE gtfs.stop_times IS 'Scheduled arrival and departure of each trip at each stop. The largest table in the feed.';
COMMENT ON COLUMN gtfs.stop_times.departure_time IS 'HH:MM:SS as published. Can exceed 24:00:00 for trips running past midnight on the previous service day.';
COMMENT ON COLUMN gtfs.stop_times.departure_secs IS 'departure_time as seconds after midnight of the service day. Use this for sorting, MIN/MAX and time arithmetic.';
COMMENT ON COLUMN gtfs.stop_times.arrival_secs IS 'arrival_time as seconds after midnight of the service day.';

CREATE TABLE IF NOT EXISTS gtfs.calendar (
    service_id  text PRIMARY KEY,
    monday      smallint NOT NULL,
    tuesday     smallint NOT NULL,
    wednesday   smallint NOT NULL,
    thursday    smallint NOT NULL,
    friday      smallint NOT NULL,
    saturday    smallint NOT NULL,
    sunday      smallint NOT NULL,
    start_date  date NOT NULL,
    end_date    date NOT NULL
);
COMMENT ON TABLE gtfs.calendar IS 'Weekly pattern of days a service_id runs between start_date and end_date (1 = runs, 0 = does not).';

CREATE TABLE IF NOT EXISTS gtfs.calendar_dates (
    service_id      text NOT NULL,
    date            date NOT NULL,
    exception_type  smallint NOT NULL,
    PRIMARY KEY (service_id, date)
);
COMMENT ON TABLE gtfs.calendar_dates IS 'Exceptions to calendar: exception_type 1 = service added on this date, 2 = service removed (e.g. public holidays, trackwork).';

CREATE TABLE IF NOT EXISTS gtfs.shapes (
    shape_id             text NOT NULL,
    shape_pt_lat         double precision NOT NULL,
    shape_pt_lon         double precision NOT NULL,
    shape_pt_sequence    integer NOT NULL,
    shape_dist_traveled  double precision,
    PRIMARY KEY (shape_id, shape_pt_sequence)
);
COMMENT ON TABLE gtfs.shapes IS 'Geographic path of a trip. Only loaded with --with-shapes; not needed for most questions.';

-- Load audit: one row per loader run. Lets answers say "timetable data as of <date>".
CREATE TABLE IF NOT EXISTS gtfs.feed_load (
    load_id     serial PRIMARY KEY,
    loaded_at   timestamptz NOT NULL DEFAULT now(),
    source      text NOT NULL,
    row_counts  jsonb NOT NULL,
    warnings    jsonb NOT NULL DEFAULT '[]'::jsonb
);
COMMENT ON TABLE gtfs.feed_load IS 'History of timetable loads. The latest loaded_at is the freshness date of all timetable answers.';
