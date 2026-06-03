-- Route all OD pairs via pgr_dijkstra on cscl_net (CSCL centerline network).
-- Mirrored from 06_route_od.sql; swaps osm_new → cscl_net, vertex_id → cl_vertex_id.
-- Output: edge_flows_cl — one row per centerline edge with correct SUM(trip_count).
-- Counts are correct by construction: each trip passes through each edge exactly once.
--
-- N_OD_PAIRS: top-N pairs by trip count to route.
--   0 = all pairs.

\set N_OD_PAIRS 0

-- ── Combinations table ────────────────────────────────────────────────────────

DROP TABLE IF EXISTS od_combinations_cl CASCADE;

CREATE TABLE od_combinations_cl AS
SELECT
    bs_start.cl_vertex_id AS source,
    bs_end.cl_vertex_id   AS target,
    op.od_id,
    op.trip_count
FROM od_pairs op
JOIN bike_stations bs_start ON op.start_station_sid = bs_start.sid
JOIN bike_stations bs_end   ON op.end_station_sid   = bs_end.sid
WHERE bs_start.cl_vertex_id IS NOT NULL
  AND bs_end.cl_vertex_id   IS NOT NULL
  AND bs_start.cl_vertex_id <> bs_end.cl_vertex_id
ORDER BY op.trip_count DESC
LIMIT CASE WHEN :N_OD_PAIRS = 0 THEN 2147483647 ELSE :N_OD_PAIRS END;

CREATE INDEX od_comb_cl_src_idx ON od_combinations_cl (source);
CREATE INDEX od_comb_cl_tgt_idx ON od_combinations_cl (target);

SELECT
    COUNT(*)              AS combinations_to_route,
    SUM(trip_count)       AS trips_in_routing
FROM od_combinations_cl;

-- ── pgr_dijkstra — single batched call over all combinations ─────────────────

DROP TABLE IF EXISTS _pgr_result_cl CASCADE;

CREATE TEMP TABLE _pgr_result_cl AS
SELECT
    c.od_id,
    c.trip_count,
    r.seq,
    r.edge,
    r.start_vid,
    r.end_vid
FROM pgr_dijkstra(
    $$SELECT id::bigint, source::bigint, target::bigint,
             cost::float8,
             reverse_cost::float8
      FROM cscl_net
      WHERE source IS NOT NULL AND target IS NOT NULL$$,
    $$SELECT source::bigint, target::bigint FROM od_combinations_cl$$,
    directed := true
) r
JOIN od_combinations_cl c ON r.start_vid = c.source AND r.end_vid = c.target;

-- ── od_routes_cl: one geometry per OD pair (for future timelapse use) ─────────

DROP TABLE IF EXISTS od_routes_cl CASCADE;

CREATE TABLE od_routes_cl AS
SELECT
    re.od_id,
    re.trip_count,
    ST_MakeLine(ST_Transform(w.geom_m, 4326) ORDER BY re.seq) AS route_geom,
    COUNT(re.edge) AS edge_count
FROM _pgr_result_cl re
JOIN cscl_net w ON re.edge = w.id
WHERE re.edge > 0
GROUP BY re.od_id, re.trip_count;

ALTER TABLE od_routes_cl ADD PRIMARY KEY (od_id);
CREATE INDEX od_routes_cl_geom_idx ON od_routes_cl USING GIST(route_geom);

-- ── edge_flows_cl: total trips per centerline edge ───────────────────────────

DROP TABLE IF EXISTS edge_flows_cl CASCADE;

CREATE TABLE edge_flows_cl AS
SELECT
    re.edge                     AS id,
    ST_Transform(w.geom_m, 4326) AS geom,
    w.street_name,
    w.rw_type,
    SUM(re.trip_count)          AS total_trips
FROM _pgr_result_cl re
JOIN cscl_net w ON re.edge = w.id
WHERE re.edge > 0
GROUP BY re.edge, w.geom_m, w.street_name, w.rw_type;

ALTER TABLE edge_flows_cl ADD PRIMARY KEY (id);
CREATE INDEX edge_flows_cl_geom_idx ON edge_flows_cl USING GIST(geom);

DROP TABLE IF EXISTS _pgr_result_cl;

-- ── Summary ───────────────────────────────────────────────────────────────────

SELECT
    (SELECT COUNT(*)              FROM od_combinations_cl)     AS od_pairs_sent,
    (SELECT COUNT(*)              FROM od_routes_cl)           AS od_routes_built,
    (SELECT SUM(trip_count)       FROM od_routes_cl)           AS trips_routed,
    (SELECT COUNT(*)              FROM edge_flows_cl)          AS cl_edges_with_flow,
    (SELECT MAX(total_trips)      FROM edge_flows_cl)          AS max_edge_flow,
    (SELECT SUM(total_trips)      FROM edge_flows_cl)          AS sum_edge_flow_passes;
