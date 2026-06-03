-- Build trips table (joining data → bike_stations) and od_pairs (directed).

DROP TABLE IF EXISTS trips CASCADE;

CREATE TABLE trips AS
SELECT
    d.ride_id,
    d.rideable_type  AS bike_type,
    d.member_casual  AS member_type,
    d.started_at     AS starting_time,
    d.ended_at       AS ending_time,
    s.sid            AS start_station_sid,
    e.sid            AS end_station_sid,
    s.geom           AS start_geom,
    s.geom_utm       AS start_geom_utm,
    e.geom           AS end_geom,
    e.geom_utm       AS end_geom_utm
FROM data d
JOIN bike_stations s
    ON d.start_station_id = s.station_id
   AND d.start_station_name = s.station_name
JOIN bike_stations e
    ON d.end_station_id = e.station_id
   AND d.end_station_name = e.station_name
WHERE d.started_at IS NOT NULL
  AND d.ended_at   IS NOT NULL
  AND d.ended_at > d.started_at
  AND (d.ended_at - d.started_at) <= INTERVAL '24 hours';

ALTER TABLE trips ADD COLUMN rid SERIAL PRIMARY KEY;

ALTER TABLE trips
    ADD CONSTRAINT fk_start_station FOREIGN KEY (start_station_sid) REFERENCES bike_stations(sid),
    ADD CONSTRAINT fk_end_station   FOREIGN KEY (end_station_sid)   REFERENCES bike_stations(sid);

CREATE INDEX trips_rid_idx        ON trips (rid);
CREATE INDEX trips_start_geom_idx ON trips USING GIST(start_geom);
CREATE INDEX trips_end_geom_idx   ON trips USING GIST(end_geom);
CREATE INDEX trips_time_idx       ON trips (starting_time);

-- ── OD pairs (directed, no self-loops) ───────────────────────────────────────

DROP TABLE IF EXISTS od_pairs CASCADE;

CREATE TABLE od_pairs AS
SELECT
    start_station_sid,
    end_station_sid,
    COUNT(*) AS trip_count
FROM trips
WHERE start_station_sid <> end_station_sid
GROUP BY start_station_sid, end_station_sid;

ALTER TABLE od_pairs ADD COLUMN od_id SERIAL PRIMARY KEY;

CREATE INDEX od_start_idx ON od_pairs (start_station_sid);
CREATE INDEX od_end_idx   ON od_pairs (end_station_sid);

-- ── Station-level trip counts (reuses legacy trips_station_counts idea) ───────

DROP VIEW IF EXISTS trips_station_counts;
CREATE VIEW trips_station_counts AS
SELECT
    start_station_sid,
    start_geom,
    COUNT(*) AS trips_per_station
FROM trips
GROUP BY start_station_sid, start_geom
ORDER BY trips_per_station DESC;

-- ── Summary ───────────────────────────────────────────────────────────────────

SELECT
    COUNT(*)                                    AS total_trips,
    COUNT(DISTINCT start_station_sid)           AS distinct_start_stations,
    COUNT(DISTINCT (start_station_sid, end_station_sid)) AS distinct_od_pairs,
    MIN(starting_time)::date                    AS period_start,
    MAX(starting_time)::date                    AS period_end
FROM trips;
