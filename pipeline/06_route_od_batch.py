"""
Batch pgRouting: routes all OD pairs in chunks to avoid OOM.
Each batch is a fresh psql subprocess — avoids SQLAlchemy/temp-table session issues.
Accumulates od_routes (one geometry per OD pair) and edge_flows (total trips per edge).

Set ROUTE_BATCH_SIZE env var to tune (default 50_000).
"""
import os
import subprocess
import sys
import time
from dotenv import load_dotenv

load_dotenv()

BATCH_SIZE  = int(os.environ.get("ROUTE_BATCH_SIZE", "50000"))
PGUSER      = os.environ["PGUSER"]
PGPASSWORD  = os.environ.get("PGPASSWORD", "")
PGHOST      = os.environ["PGHOST"]
PGPORT      = os.environ["PGPORT"]
PGDATABASE  = os.environ["PGDATABASE"]

PSQL = [
    "psql",
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


# ── Setup empty accumulator tables ───────────────────────────────────────────

run_sql("""
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
""", label="setup")

# ── Count total OD pairs ──────────────────────────────────────────────────────

total = int(subprocess.run(
    PSQL + ["-tA", "-c", "SELECT COUNT(*) FROM od_combinations;"],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
).stdout.decode().strip())
n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
print(f"Routing {total:,} OD pairs — {n_batches} batches of {BATCH_SIZE:,}", flush=True)

# ── Process batches ───────────────────────────────────────────────────────────

t_start = time.time()

for batch_num in range(1, n_batches + 1):
    offset   = (batch_num - 1) * BATCH_SIZE
    t_batch  = time.time()
    label    = f"batch {batch_num}/{n_batches}"

    batch_sql = f"""
-- Slice the combinations for this batch (deterministic order by od_id)
CREATE TEMP TABLE _batch_combs AS
SELECT source, target, od_id, trip_count
FROM od_combinations
ORDER BY od_id
LIMIT {BATCH_SIZE} OFFSET {offset};

-- Route this batch
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

-- Accumulate edge-level flow totals
INSERT INTO edge_flows (id, geom, total_trips)
SELECT re.edge, w.geom, SUM(re.trip_count)
FROM _batch_result re
JOIN osm_new w ON re.edge = w.id
WHERE re.edge > 0
GROUP BY re.edge, w.geom
ON CONFLICT (id) DO UPDATE
    SET total_trips = edge_flows.total_trips + EXCLUDED.total_trips;
"""

    print(f"  {label}: offset {offset:,}…", end=" ", flush=True)
    run_sql(batch_sql, label=label)
    elapsed = time.time() - t_batch
    done    = min(offset + BATCH_SIZE, total)
    print(f"done  ({done:,}/{total:,}  {elapsed:.0f}s)", flush=True)

# ── Final indexes ─────────────────────────────────────────────────────────────

print("Creating indexes…", flush=True)
run_sql("""
CREATE INDEX od_routes_geom_idx  ON od_routes  USING GIST(route_geom);
CREATE INDEX edge_flows_geom_idx ON edge_flows USING GIST(geom);
""", label="indexes")

# ── Summary ───────────────────────────────────────────────────────────────────

summary = run_sql("""
SELECT
    (SELECT COUNT(*)         FROM od_routes)  AS routes,
    (SELECT COUNT(*)         FROM edge_flows) AS edges,
    (SELECT MAX(total_trips) FROM edge_flows) AS max_trips;
""", label="summary")

total_s = time.time() - t_start
print(f"Done in {total_s/60:.1f} min:")
print(summary)
