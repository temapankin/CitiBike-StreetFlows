-- Route distinct OD pairs via pgr_dijkstra combinations API (directed).
-- All pairs batched in one call — far faster than one call per pair.
-- osm2pgrouting v3 columns: id (PK), geom, source, target, cost, reverse_cost, one_way
--
-- N_OD_PAIRS: top-N pairs by trip count to route.
--   0 = all pairs. Start with 5000 for a quick demo.

\set N_OD_PAIRS 0

-- ── Combinations table ────────────────────────────────────────────────────────

DROP TABLE IF EXISTS od_combinations CASCADE;

CREATE TABLE od_combinations AS
SELECT
    bs_start.vertex_id AS source,
    bs_end.vertex_id   AS target,
    op.od_id,
    op.trip_count
FROM od_pairs op
JOIN bike_stations bs_start ON op.start_station_sid = bs_start.sid
JOIN bike_stations bs_end   ON op.end_station_sid   = bs_end.sid
WHERE bs_start.vertex_id IS NOT NULL
  AND bs_end.vertex_id   IS NOT NULL
  AND bs_start.vertex_id <> bs_end.vertex_id
ORDER BY op.trip_count DESC
LIMIT CASE WHEN :N_OD_PAIRS = 0 THEN 2147483647 ELSE :N_OD_PAIRS END;

CREATE INDEX od_comb_src_idx ON od_combinations (source);
CREATE INDEX od_comb_tgt_idx ON od_combinations (target);

SELECT COUNT(*) AS combinations_to_route FROM od_combinations;

-- ── pgr_dijkstra — single batched call over all combinations ─────────────────

DROP TABLE IF EXISTS _pgr_result CASCADE;

CREATE TEMP TABLE _pgr_result AS
SELECT
    c.od_id,
    c.trip_count,
    r.seq,
    r.edge,
    r.start_vid,
    r.end_vid
FROM pgr_dijkstra(
    $$SELECT id, source, target,
             cost,
             reverse_cost
      FROM osm_new$$,
    $$SELECT source, target FROM od_combinations$$,
    directed := true
) r
JOIN od_combinations c ON r.start_vid = c.source AND r.end_vid = c.target;

-- ── od_routes: one geometry per OD pair ──────────────────────────────────────

DROP TABLE IF EXISTS od_routes CASCADE;

CREATE TABLE od_routes AS
SELECT
    re.od_id,
    re.trip_count,
    ST_MakeLine(w.geom ORDER BY re.seq) AS route_geom,
    COUNT(re.edge)                       AS edge_count
FROM _pgr_result re
JOIN osm_new w ON re.edge = w.id
WHERE re.edge > 0
GROUP BY re.od_id, re.trip_count;

ALTER TABLE od_routes ADD PRIMARY KEY (od_id);
CREATE INDEX od_routes_geom_idx ON od_routes USING GIST(route_geom);

-- ── edge_flows: total trips per street edge ───────────────────────────────────

DROP TABLE IF EXISTS edge_flows CASCADE;

CREATE TABLE edge_flows AS
SELECT
    re.edge            AS id,
    w.geom,
    SUM(re.trip_count) AS total_trips
FROM _pgr_result re
JOIN osm_new w ON re.edge = w.id
WHERE re.edge > 0
GROUP BY re.edge, w.geom;

ALTER TABLE edge_flows ADD PRIMARY KEY (id);
CREATE INDEX edge_flows_geom_idx ON edge_flows USING GIST(geom);

DROP TABLE IF EXISTS _pgr_result;

-- ── Summary ───────────────────────────────────────────────────────────────────

SELECT
    (SELECT COUNT(*) FROM od_combinations)     AS od_pairs_routed,
    (SELECT COUNT(*) FROM od_routes)           AS od_routes_built,
    (SELECT COUNT(*) FROM edge_flows)          AS edges_with_flow,
    (SELECT MAX(total_trips) FROM edge_flows)  AS max_edge_flow;
