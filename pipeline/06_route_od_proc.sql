-- Batch pgRouting via a stored procedure (supports COMMIT between batches).
-- Avoids the single-call OOM while accumulating od_routes and edge_flows
-- incrementally across committed batches.

-- ── Accumulator tables ────────────────────────────────────────────────────────

DROP TABLE IF EXISTS od_routes CASCADE;
CREATE TABLE od_routes (
    od_id      BIGINT PRIMARY KEY,
    trip_count BIGINT,
    route_geom GEOMETRY(LineString, 4326),
    edge_count BIGINT
);

DROP TABLE IF EXISTS edge_flows CASCADE;
CREATE TABLE edge_flows (
    id          BIGINT PRIMARY KEY,
    geom        GEOMETRY(LineString, 4326),
    total_trips BIGINT
);

-- ── Routing procedure ─────────────────────────────────────────────────────────

CREATE OR REPLACE PROCEDURE route_od_batched(batch_size INT DEFAULT 50000)
LANGUAGE plpgsql AS $proc$
DECLARE
    total_pairs BIGINT;
    offset_val  BIGINT := 0;
    batch_num   INT    := 0;
    routes_ins  BIGINT;
    edges_ins   BIGINT;
BEGIN
    SELECT COUNT(*) INTO total_pairs FROM od_combinations;
    RAISE NOTICE 'Routing % OD pairs in batches of %', total_pairs, batch_size;

    WHILE offset_val < total_pairs LOOP
        batch_num := batch_num + 1;

        -- Slice this batch (deterministic order)
        DROP TABLE IF EXISTS _batch_combs;
        CREATE TEMP TABLE _batch_combs AS
        SELECT source, target, od_id, trip_count
        FROM od_combinations
        ORDER BY od_id
        LIMIT batch_size OFFSET offset_val;

        -- Route via pgr_dijkstra; join back for od_id + trip_count
        DROP TABLE IF EXISTS _batch_result;
        CREATE TEMP TABLE _batch_result AS
        SELECT c.od_id, c.trip_count, r.seq, r.edge, r.start_vid, r.end_vid
        FROM pgr_dijkstra(
            $$SELECT id, source, target, cost, reverse_cost FROM osm_new$$,
            $$SELECT source, target FROM _batch_combs$$,
            directed := true
        ) r
        JOIN _batch_combs c ON r.start_vid = c.source AND r.end_vid = c.target;

        -- Accumulate route geometries
        INSERT INTO od_routes (od_id, trip_count, route_geom, edge_count)
        SELECT re.od_id,
               re.trip_count,
               ST_MakeLine(w.geom ORDER BY re.seq),
               COUNT(re.edge)
        FROM _batch_result re
        JOIN osm_new w ON re.edge = w.id
        WHERE re.edge > 0
        GROUP BY re.od_id, re.trip_count
        ON CONFLICT (od_id) DO NOTHING;
        GET DIAGNOSTICS routes_ins = ROW_COUNT;

        -- Accumulate edge-level flow totals
        INSERT INTO edge_flows (id, geom, total_trips)
        SELECT re.edge, w.geom, SUM(re.trip_count)
        FROM _batch_result re
        JOIN osm_new w ON re.edge = w.id
        WHERE re.edge > 0
        GROUP BY re.edge, w.geom
        ON CONFLICT (id) DO UPDATE
            SET total_trips = edge_flows.total_trips + EXCLUDED.total_trips;
        GET DIAGNOSTICS edges_ins = ROW_COUNT;

        COMMIT;  -- persist this batch; frees pgRouting memory

        offset_val := offset_val + batch_size;
        RAISE NOTICE 'Batch %: %/% pairs done — +% routes, +% edge rows',
            batch_num, LEAST(offset_val, total_pairs), total_pairs,
            routes_ins, edges_ins;
    END LOOP;

    -- Final spatial indexes
    CREATE INDEX od_routes_geom_idx  ON od_routes  USING GIST(route_geom);
    CREATE INDEX edge_flows_geom_idx ON edge_flows USING GIST(geom);

    RAISE NOTICE 'Done. % routes | % edge flows | max trips/edge: %',
        (SELECT COUNT(*)         FROM od_routes),
        (SELECT COUNT(*)         FROM edge_flows),
        (SELECT MAX(total_trips) FROM edge_flows);
END $proc$;

-- ── Execute ───────────────────────────────────────────────────────────────────

CALL route_od_batched(50000);
