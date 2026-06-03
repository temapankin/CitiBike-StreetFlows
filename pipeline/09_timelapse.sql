-- Build timelapse tables for Parquet export.
-- Reads from od_routes_cl (centerline routes, bridges included) and trips.
--
-- Outputs:
--   tl_routes(route_id, geom)             – compact int id, simplified geometry
--   tl_route_points(route_id, seq, lon, lat) – per-vertex rows for DuckDB aggregation
--   tl_trips(route_id, t0, dur, day)      – animation-clock seconds + calendar day
--   tl_meta                               – single-row period/count summary

-- ── Constants (editable) ──────────────────────────────────────────────────────
-- Simplification tolerance in degrees (~5 m at NYC latitude ≈ 0.000045)
\set SIMPLIFY_TOL 0.000045
-- Animation clock length in seconds
\set ANIM_DURATION_S 180
-- Drop stray pre-summer trips: the dataset is June–August 2023, but a handful of
-- 2023-05-31 trips leaked in. Exclude anything before this date everywhere below.
\set START_DATE '2023-06-01'

-- ── tl_routes ─────────────────────────────────────────────────────────────────
-- dense_rank gives compact 1-based int ids; simplify reduces vertex count.

DROP TABLE IF EXISTS tl_routes CASCADE;

CREATE TABLE tl_routes AS
SELECT
    DENSE_RANK() OVER (ORDER BY od_id)::int AS route_id,
    od_id,
    ST_RemoveRepeatedPoints(
        ST_SimplifyPreserveTopology(route_geom, :SIMPLIFY_TOL)
    ) AS geom
FROM od_routes_cl
WHERE route_geom IS NOT NULL
  AND ST_NPoints(route_geom) >= 2;

ALTER TABLE tl_routes ADD PRIMARY KEY (route_id);
CREATE INDEX tl_routes_od_idx ON tl_routes (od_id);
CREATE INDEX tl_routes_geom_idx ON tl_routes USING GIST(geom);

SELECT
    COUNT(*)                          AS routes_total,
    SUM(ST_NPoints(geom))             AS total_vertices_after,
    AVG(ST_NPoints(geom))::numeric(8,1) AS avg_vertices_per_route
FROM tl_routes;

-- ── tl_route_points ───────────────────────────────────────────────────────────
-- One row per vertex; DuckDB will GROUP BY route_id to build coordinate arrays.

DROP TABLE IF EXISTS tl_route_points CASCADE;

CREATE TABLE tl_route_points AS
SELECT
    r.route_id,
    (dp.path)[1] AS seq,
    ST_X(dp.geom)::real AS lon,
    ST_Y(dp.geom)::real AS lat
FROM tl_routes r
CROSS JOIN LATERAL ST_DumpPoints(r.geom) AS dp;

CREATE INDEX tl_rp_route_idx ON tl_route_points (route_id, seq);

SELECT COUNT(*) AS total_vertices FROM tl_route_points;

-- ── tl_trips ──────────────────────────────────────────────────────────────────
-- Animation clock: t0 and dur in seconds within [0, ANIM_DURATION_S].
-- day column enables Parquet partitioning by calendar day.

DROP TABLE IF EXISTS tl_trips CASCADE;

CREATE TABLE tl_trips AS
WITH bounds AS (
    SELECT
        MIN(starting_time) AS t_min,
        MAX(ending_time)   AS t_max,
        EXTRACT(EPOCH FROM (MAX(ending_time) - MIN(starting_time))) AS span_s
    FROM trips
    WHERE starting_time::date >= DATE :'START_DATE'
),
joined AS (
    SELECT
        tr.route_id,
        t.starting_time,
        t.ending_time,
        b.t_min,
        b.span_s
    FROM trips t
    JOIN od_pairs op
        ON t.start_station_sid = op.start_station_sid
       AND t.end_station_sid   = op.end_station_sid
    JOIN tl_routes tr ON op.od_id = tr.od_id
    CROSS JOIN bounds b
    WHERE t.starting_time IS NOT NULL
      AND t.ending_time   IS NOT NULL
      AND t.starting_time::date >= DATE :'START_DATE'
)
SELECT
    route_id::int,
    (EXTRACT(EPOCH FROM (starting_time - t_min)) / span_s * :ANIM_DURATION_S)::real AS t0,
    GREATEST(
        0.1,
        (EXTRACT(EPOCH FROM (ending_time - starting_time)) / span_s * :ANIM_DURATION_S)
    )::real AS dur,
    starting_time::date AS day
FROM joined
ORDER BY t0;

CREATE INDEX tl_trips_route_idx ON tl_trips (route_id);
CREATE INDEX tl_trips_t0_idx    ON tl_trips (t0);
CREATE INDEX tl_trips_day_idx   ON tl_trips (day);

-- ── tl_meta ───────────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS tl_meta CASCADE;

CREATE TABLE tl_meta AS
WITH bounds AS (
    SELECT MIN(starting_time) AS t_min, MAX(ending_time) AS t_max
    FROM trips WHERE starting_time::date >= DATE :'START_DATE'
)
SELECT
    b.t_min                             AS period_start,
    b.t_max                             AS period_end,
    :ANIM_DURATION_S::int               AS anim_duration_s,
    (SELECT COUNT(*) FROM tl_routes)    AS n_routes,
    (SELECT COUNT(*) FROM tl_trips)     AS n_trips
FROM bounds b;

SELECT * FROM tl_meta;

-- ── Verification gates ────────────────────────────────────────────────────────

SELECT
    'trips_in_tl_trips'                 AS check,
    COUNT(*)                            AS value,
    '~10.6M expected'                   AS note
FROM tl_trips
UNION ALL
SELECT
    'routes_with_no_trips',
    COUNT(*),
    'should be 0 if all routes used by at least one trip'
FROM tl_routes r
WHERE NOT EXISTS (SELECT 1 FROM tl_trips tt WHERE tt.route_id = r.route_id)
UNION ALL
SELECT
    't0_min',
    MIN(t0)::int,
    'should be 0'
FROM tl_trips
UNION ALL
SELECT
    't0_max_approx',
    MAX(t0)::int,
    'should be <= 180'
FROM tl_trips;
