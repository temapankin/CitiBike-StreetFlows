"""
Batch pgRouting on cscl_net: routes all OD pairs in chunks to avoid OOM.
Mirrors 06_route_od_batch.py but uses cscl_net / cl_vertex_id instead of osm_new.
Accumulates od_routes_cl and edge_flows_cl tables incrementally.

Set ROUTE_BATCH_SIZE env var to tune (default 50_000).
"""

import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE = int(os.environ.get("ROUTE_BATCH_SIZE", "50000"))
PGUSER     = os.environ["PGUSER"]
PGPASSWORD = os.environ.get("PGPASSWORD", "")
PGHOST     = os.environ["PGHOST"]
PGPORT     = os.environ["PGPORT"]
PGDATABASE = os.environ["PGDATABASE"]

PSQL = [
    "/opt/homebrew/bin/psql",
    f"--username={PGUSER}",
    f"--host={PGHOST}",
    f"--port={PGPORT}",
    f"--dbname={PGDATABASE}",
    "-v", "ON_ERROR_STOP=1",
]

env = {**os.environ, "PGPASSWORD": PGPASSWORD}


def run_sql(sql: str, label: str = "") -> str:
    result = subprocess.run(
        PSQL, input=sql.encode(), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=7200,
    )
    if result.returncode != 0:
        print(f"\n[psql error{' in ' + label if label else ''}]", file=sys.stderr)
        print(result.stderr.decode(), file=sys.stderr)
        sys.exit(1)
    return result.stdout.decode()


# ── Build od_combinations_cl (stations → cl_vertex_id) ───────────────────────

print("Building od_combinations_cl…", flush=True)
run_sql("""
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
ORDER BY op.trip_count DESC;

CREATE INDEX od_comb_cl_src_idx ON od_combinations_cl (source);
CREATE INDEX od_comb_cl_tgt_idx ON od_combinations_cl (target);
""", label="od_combinations_cl")

# ── Setup empty accumulator tables ───────────────────────────────────────────

print("Setting up accumulator tables…", flush=True)
run_sql("""
DROP TABLE IF EXISTS od_routes_cl CASCADE;
CREATE TABLE od_routes_cl (
    od_id      BIGINT PRIMARY KEY,
    trip_count BIGINT,
    route_geom GEOMETRY(LineString, 4326),
    edge_count BIGINT
);

DROP TABLE IF EXISTS edge_flows_cl CASCADE;
CREATE TABLE edge_flows_cl (
    id          BIGINT PRIMARY KEY,
    geom        GEOMETRY(LineString, 4326),
    street_name TEXT,
    rw_type     INTEGER,
    total_trips BIGINT
);
""", label="setup")

# ── Count total OD pairs ──────────────────────────────────────────────────────

total_str = subprocess.run(
    PSQL + ["-tA", "-c", "SELECT COUNT(*) FROM od_combinations_cl;"],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
).stdout.decode().strip()
total = int(total_str)
n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
print(f"Routing {total:,} OD pairs — {n_batches} batches of {BATCH_SIZE:,}", flush=True)

# ── Process batches ───────────────────────────────────────────────────────────

t_start = time.time()

for batch_num in range(1, n_batches + 1):
    offset  = (batch_num - 1) * BATCH_SIZE
    t_batch = time.time()
    label   = f"batch {batch_num}/{n_batches}"

    batch_sql = f"""
-- Slice combinations for this batch
CREATE TEMP TABLE _batch_combs AS
SELECT source, target, od_id, trip_count
FROM od_combinations_cl
ORDER BY od_id
LIMIT {BATCH_SIZE} OFFSET {offset};

-- Route this batch on cscl_net
CREATE TEMP TABLE _batch_result AS
SELECT c.od_id, c.trip_count, r.seq, r.edge, r.start_vid, r.end_vid
FROM pgr_dijkstra(
    $$SELECT id::bigint, source::bigint, target::bigint,
             cost::float8, reverse_cost::float8
      FROM cscl_net
      WHERE source IS NOT NULL AND target IS NOT NULL$$,
    $$SELECT source::bigint, target::bigint FROM _batch_combs$$,
    directed := true
) r
JOIN _batch_combs c ON r.start_vid = c.source AND r.end_vid = c.target;

-- Accumulate route geometries (WGS84 via geom, not geom_m)
INSERT INTO od_routes_cl (od_id, trip_count, route_geom, edge_count)
SELECT re.od_id,
       re.trip_count,
       ST_MakeLine(w.geom ORDER BY re.seq),
       COUNT(re.edge)
FROM _batch_result re
JOIN cscl_net w ON re.edge = w.id
WHERE re.edge > 0
GROUP BY re.od_id, re.trip_count
ON CONFLICT (od_id) DO NOTHING;

-- Accumulate edge-level flow totals
INSERT INTO edge_flows_cl (id, geom, street_name, rw_type, total_trips)
SELECT re.edge,
       w.geom,
       w.street_name,
       w.rw_type,
       SUM(re.trip_count)
FROM _batch_result re
JOIN cscl_net w ON re.edge = w.id
WHERE re.edge > 0
GROUP BY re.edge, w.geom, w.street_name, w.rw_type
ON CONFLICT (id) DO UPDATE
    SET total_trips = edge_flows_cl.total_trips + EXCLUDED.total_trips;

DROP TABLE _batch_combs;
DROP TABLE _batch_result;
"""

    print(f"  {label}: offset {offset:,}…", end=" ", flush=True)
    run_sql(batch_sql, label=label)
    elapsed = time.time() - t_batch
    done    = min(offset + BATCH_SIZE, total)
    pct     = done / total * 100
    print(f"done  ({done:,}/{total:,}  {pct:.1f}%  {elapsed:.0f}s)", flush=True)

# ── Final indexes ─────────────────────────────────────────────────────────────

print("Creating indexes…", flush=True)
run_sql("""
CREATE INDEX od_routes_cl_geom_idx   ON od_routes_cl   USING GIST(route_geom);
CREATE INDEX edge_flows_cl_geom_idx  ON edge_flows_cl  USING GIST(geom);
""", label="indexes")

# ── Sanity gates ──────────────────────────────────────────────────────────────

print("\nRunning sanity gates…", flush=True)
summary = run_sql("""
SELECT
    (SELECT COUNT(*)          FROM od_routes_cl)     AS routes_built,
    (SELECT SUM(trip_count)   FROM od_routes_cl)     AS trips_routed,
    (SELECT COUNT(*)          FROM edge_flows_cl)    AS cl_edges_with_flow,
    (SELECT MAX(total_trips)  FROM edge_flows_cl)    AS max_edge_flow,
    (SELECT SUM(total_trips)  FROM edge_flows_cl)    AS sum_flow_passes,
    (SELECT COUNT(*)
     FROM od_routes_cl r
     JOIN od_combinations_cl c ON c.od_id = r.od_id
     JOIN bike_stations bs ON bs.cl_vertex_id = c.source
     JOIN bike_stations be ON be.cl_vertex_id = c.target
     WHERE bs.borough <> be.borough)                AS cross_boro_routed,
    (SELECT SUM(total_trips)
     FROM edge_flows_cl
     WHERE upper(street_name) LIKE '%BROOKLYN BRG%'
        OR upper(street_name) LIKE '%WILLIAMSBURG BRG%'
        OR upper(street_name) LIKE '%QUEENSBORO%')  AS bridge_trips;
""", label="summary")

total_s = time.time() - t_start
print(f"\nRouting done in {total_s/60:.1f} min:")
print(summary)
