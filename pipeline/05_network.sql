-- Clip the osm2pgrouting network to NYC boroughs, index everything, snap stations.
-- osm2pgrouting v3 schema: ways(id, geom, source, target, cost, reverse_cost, one_way)
--                          ways_vertices_pgr(id, geom)

-- ── GIST index on vertices (needed for fast KNN snap) ────────────────────────
-- CREATE INDEX CONCURRENTLY so it can be re-run safely even if already present.

CREATE INDEX IF NOT EXISTS wv_geom_idx ON ways_vertices_pgr USING GIST(geom);

-- ── Clip ways to NYC borough polygons ────────────────────────────────────────

DROP TABLE IF EXISTS osm_new CASCADE;

CREATE TABLE osm_new AS
SELECT w.*
FROM ways w
JOIN boroughs b ON ST_Within(w.geom, b.geom);

ALTER TABLE osm_new ADD PRIMARY KEY (id);
CREATE INDEX osm_new_geom_idx   ON osm_new USING GIST(geom);
CREATE INDEX osm_new_source_idx ON osm_new (source);
CREATE INDEX osm_new_target_idx ON osm_new (target);

SELECT COUNT(*) AS nyc_edges FROM osm_new;

-- ── Snap each station to its nearest network vertex (KNN, uses GIST index) ───

ALTER TABLE bike_stations ADD COLUMN IF NOT EXISTS vertex_id BIGINT;

-- Reset any partial results from a previous failed run
UPDATE bike_stations SET vertex_id = NULL;

UPDATE bike_stations bs
SET vertex_id = v.id
FROM (
    SELECT DISTINCT ON (bs2.sid)
        bs2.sid,
        v2.id
    FROM bike_stations bs2
    CROSS JOIN LATERAL (
        SELECT id
        FROM ways_vertices_pgr
        ORDER BY geom <-> bs2.geom
        LIMIT 1
    ) v2
) v
WHERE bs.sid = v.sid;

DO $$
DECLARE unsnapped INT;
BEGIN
    SELECT COUNT(*) INTO unsnapped FROM bike_stations WHERE vertex_id IS NULL;
    IF unsnapped > 0 THEN
        RAISE WARNING '% stations could not be snapped to a network vertex', unsnapped;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS bs_vertex_idx ON bike_stations (vertex_id);

-- ── Summary ───────────────────────────────────────────────────────────────────

SELECT
    (SELECT COUNT(*) FROM osm_new)                                        AS network_edges,
    (SELECT COUNT(*) FROM ways_vertices_pgr)                              AS network_vertices,
    (SELECT COUNT(*) FROM bike_stations WHERE vertex_id IS NOT NULL)      AS stations_snapped,
    (SELECT COUNT(*) FROM bike_stations WHERE vertex_id IS NULL)          AS stations_unsnapped;
