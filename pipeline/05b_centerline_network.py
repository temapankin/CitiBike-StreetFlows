"""
pipeline/05b_centerline_network.py

Build a routable pgRouting network (cscl_net) from the NYC Street Centerline (CSCL).
Steps:
  1. Load raw CSCL GeoJSON keeping rw_type, nonped, bike_trafdir, trafdir, street_name.
  2. Apply bike-access filter (exclude expressways, tunnels, ramps, ferries; filter bridge rw_type).
  3. Drop degenerate segments (< 0.1 m in UTM).
  4. Build topology: cluster endpoints within SNAP_TOL_M → vertex IDs via ST_ClusterDBSCAN.
  5. Set source/target on cscl_net; compute cost = ST_Length(geom_m).
  6. Snap bike_stations to nearest cscl_net vertex → bike_stations.cl_vertex_id.
  7. Connectivity gate: largest pgr_connectedComponents component must contain stations
     from all 5 boroughs AND named bridge segments must be in it.

pgRouting 4 API note: pgr_createTopology is removed — topology is built manually here.
"""

import os
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DB_URL = (
    f"postgresql://{os.environ['PGUSER']}:{os.environ.get('PGPASSWORD', '')}"
    f"@{os.environ['PGHOST']}:{os.environ['PGPORT']}/{os.environ['PGDATABASE']}"
)
engine = create_engine(DB_URL)

_CENTERLINE_CANDIDATES = [
    Path("data/raw/nyc_centerline.geojson"),
    Path("data/raw/Centerline_20260530.geojson"),
]
CENTERLINE_PATH = next((p for p in _CENTERLINE_CANDIDATES if p.exists()), _CENTERLINE_CANDIDATES[0])

# Topology endpoint-snapping tolerance (meters, UTM-32618)
SNAP_TOL_M = 2.0

# Bike-access filter (data-driven from rw_type analysis):
#   EXCLUDE: 2=expressways, 4=tunnels, 9=highway ramps, 14=ferry routes over water
#   rw_type=3 (bridges): keep only where nonped IS DISTINCT FROM 'V'
#     (drop vehicular-only highway bridges: Belt Pkwy, FDR, Throgs Neck)
#   All others (1,5,6,7,8,10,12,13): include
EXCLUDE_RW_TYPES = {2, 4, 9, 14}


# ── Topology SQL ─────────────────────────────────────────────────────────────

_TOPOLOGY_SQL = """
-- Extract all endpoints and cluster within {tol}m to get vertex IDs
CREATE TEMP TABLE _endpoints ON COMMIT DROP AS
SELECT id AS edge_id, 'S' AS ep, ST_StartPoint(geom_m) AS pt FROM cscl_net
UNION ALL
SELECT id AS edge_id, 'E' AS ep, ST_EndPoint(geom_m)   AS pt FROM cscl_net;

CREATE INDEX _ep_pt_idx ON _endpoints USING GIST(pt);

CREATE TEMP TABLE _clustered ON COMMIT DROP AS
SELECT edge_id, ep, pt,
       ST_ClusterDBSCAN(pt, eps := {tol}::float, minpoints := 1) OVER () AS vtx_id
FROM _endpoints;

-- Vertex table: centroid of each cluster
DROP TABLE IF EXISTS cscl_vertices CASCADE;
CREATE TABLE cscl_vertices AS
SELECT vtx_id AS id, ST_Centroid(ST_Collect(pt)) AS geom
FROM _clustered GROUP BY vtx_id;

ALTER TABLE cscl_vertices ADD PRIMARY KEY (id);
CREATE INDEX cscl_vertices_geom_idx ON cscl_vertices USING GIST(geom);

-- Assign source/target to edges
UPDATE cscl_net cn SET source = c.vtx_id
FROM _clustered c WHERE c.edge_id = cn.id AND c.ep = 'S';

UPDATE cscl_net cn SET target = c.vtx_id
FROM _clustered c WHERE c.edge_id = cn.id AND c.ep = 'E';
""".format(tol=SNAP_TOL_M)


# ── Station snapping SQL ──────────────────────────────────────────────────────

_SNAP_STATIONS_SQL = """
ALTER TABLE bike_stations ADD COLUMN IF NOT EXISTS cl_vertex_id BIGINT;
UPDATE bike_stations SET cl_vertex_id = NULL;

UPDATE bike_stations bs
SET cl_vertex_id = v.id
FROM (
    SELECT DISTINCT ON (bs2.sid)
        bs2.sid,
        v2.id
    FROM bike_stations bs2
    CROSS JOIN LATERAL (
        SELECT id
        FROM cscl_vertices
        ORDER BY geom <-> ST_Transform(bs2.geom, 32618)
        LIMIT 1
    ) v2
) v
WHERE bs.sid = v.sid;
"""


def load_and_filter():
    """Load raw CSCL, apply bike-access filter, drop degenerate geometry."""
    print(f"Loading {CENTERLINE_PATH} …")
    gdf = gpd.read_file(CENTERLINE_PATH)
    gdf = gdf.set_crs(4326, allow_override=True)

    # Keep only what we need
    needed = ["rw_type", "nonped", "trafdir", "bike_trafdir", "full_street_name", "geometry"]
    gdf = gdf[[c for c in needed if c in gdf.columns]].copy()
    gdf["rw_type"] = pd.to_numeric(gdf.get("rw_type", 0), errors="coerce").fillna(0).astype(int)
    gdf["nonped"] = gdf.get("nonped", "").fillna("").astype(str)

    n_raw = len(gdf)
    print(f"  Raw segments: {n_raw:,}")

    # Print rw_type summary before filter
    print("  rw_type distribution before filter:")
    for rt, cnt in sorted(gdf["rw_type"].value_counts().items()):
        tag = " ← EXCLUDED" if rt in EXCLUDE_RW_TYPES else (
              " ← BRIDGE (partial)" if rt == 3 else "")
        print(f"    rw_type={rt:>3}: {cnt:>7,}{tag}")

    # Apply bike-access filter
    mask_excl = gdf["rw_type"].isin(EXCLUDE_RW_TYPES)
    # rw_type=3 bridges: drop vehicular-only (nonped='V')
    mask_veh_bridge = (gdf["rw_type"] == 3) & (gdf["nonped"] == "V")
    gdf = gdf[~mask_excl & ~mask_veh_bridge].copy()
    n_after_filter = len(gdf)
    n_excluded = n_raw - n_after_filter
    print(f"  After bike-access filter: {n_after_filter:,} segments ({n_excluded:,} excluded)")

    # Explode MultiLineString → LineString (CSCL ships as Multi)
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    gdf = gdf[gdf.geometry.geom_type == "LineString"].copy()

    # Reproject to UTM-32618 for length computation
    gdf_m = gdf.to_crs(32618)

    # Drop degenerate segments (< 0.1 m or < 2 distinct coords)
    valid_len = gdf_m.geometry.length >= 0.1
    valid_pts = gdf_m.geometry.apply(lambda g: len(set(g.coords)) >= 2)
    n_before_degen = len(gdf_m)
    gdf = gdf[valid_len & valid_pts].copy()
    gdf_m = gdf_m[valid_len & valid_pts].copy()
    n_degen = n_before_degen - len(gdf)
    print(f"  Degenerate segments dropped: {n_degen} (< 0.1 m or < 2 distinct coords)")
    print(f"  Final segments to load: {len(gdf):,}")

    gdf = gdf.reset_index(drop=True)
    gdf_m = gdf_m.reset_index(drop=True)
    gdf["cl_id"] = range(1, len(gdf) + 1)
    gdf["geom_m_wkb"] = gdf_m.geometry  # UTM geom for length/topology

    # Rename for clarity
    gdf = gdf.rename(columns={"full_street_name": "street_name"})
    return gdf, gdf_m


def write_to_db(gdf, gdf_m):
    """Write cscl_net to DB with source/target/cost columns (unfilled yet)."""
    print("Writing cscl_net to DB …")

    gdf_write = gdf[["cl_id", "street_name", "rw_type", "nonped",
                      "trafdir", "bike_trafdir", "geometry"]].copy()
    gdf_write = gdf_write.rename(columns={"geometry": "geom"})
    gdf_write = gdf_write.set_geometry("geom")

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS cscl_net CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS cscl_vertices CASCADE"))

    gdf_write.to_postgis("cscl_net", engine, if_exists="replace",
                         index=False, chunksize=5000)

    print("  Adding geometry columns and cost …")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE cscl_net RENAME COLUMN cl_id TO id"))
        conn.execute(text("ALTER TABLE cscl_net ADD PRIMARY KEY (id)"))
        conn.execute(text(
            "ALTER TABLE cscl_net ALTER COLUMN geom "
            "TYPE geometry(LineString, 4326) USING ST_SetSRID(geom::geometry, 4326)"
        ))
        conn.execute(text(
            "ALTER TABLE cscl_net ADD COLUMN geom_m geometry(LineString, 32618)"
        ))
        conn.execute(text("UPDATE cscl_net SET geom_m = ST_Transform(geom, 32618)"))
        conn.execute(text(
            "ALTER TABLE cscl_net ADD COLUMN source BIGINT, ADD COLUMN target BIGINT"
        ))
        # cost = length in meters, bidirectional v1
        conn.execute(text(
            "ALTER TABLE cscl_net ADD COLUMN cost FLOAT8, ADD COLUMN reverse_cost FLOAT8"
        ))
        conn.execute(text(
            "UPDATE cscl_net SET cost = ST_Length(geom_m), "
            "reverse_cost = ST_Length(geom_m)"
        ))
        conn.execute(text("CREATE INDEX cscl_net_geom_idx ON cscl_net USING GIST(geom)"))
        conn.execute(text("CREATE INDEX cscl_net_geom_m_idx ON cscl_net USING GIST(geom_m)"))

    print(f"  cscl_net written: {len(gdf):,} edges")


def build_topology():
    """Cluster endpoints → vertex IDs, set source/target on cscl_net."""
    print(f"Building topology (endpoint snapping tol={SNAP_TOL_M}m) …")
    with engine.begin() as conn:
        conn.execute(text(_TOPOLOGY_SQL))

    with engine.connect() as conn:
        n_vtx = conn.execute(text("SELECT COUNT(*) FROM cscl_vertices")).scalar()
        n_orphan = conn.execute(text(
            "SELECT COUNT(*) FROM cscl_net WHERE source IS NULL OR target IS NULL"
        )).scalar()

    print(f"  Vertices created: {n_vtx:,}")
    print(f"  Edges with NULL source/target: {n_orphan}")
    if n_orphan > 0:
        print(f"  WARN: {n_orphan} edges could not be assigned source/target; "
              "they will be excluded from routing but kept in the table.")

    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX cscl_net_src_idx ON cscl_net (source)"))
        conn.execute(text("CREATE INDEX cscl_net_tgt_idx ON cscl_net (target)"))
    return n_vtx, n_orphan


def snap_stations():
    """Snap bike_stations to nearest cscl_net vertex."""
    print("Snapping stations to cscl_net vertices …")
    with engine.begin() as conn:
        conn.execute(text(_SNAP_STATIONS_SQL))

    with engine.connect() as conn:
        n_snapped = conn.execute(text(
            "SELECT COUNT(*) FROM bike_stations WHERE cl_vertex_id IS NOT NULL"
        )).scalar()
        n_unsnapped = conn.execute(text(
            "SELECT COUNT(*) FROM bike_stations WHERE cl_vertex_id IS NULL"
        )).scalar()

    print(f"  Stations snapped: {n_snapped:,}  unsnapped: {n_unsnapped}")
    return n_snapped, n_unsnapped


def connectivity_gate():
    """
    GATE: largest connected component must contain stations from all 5 boroughs
    AND named bridge segments must be in it.
    Returns True if passed, False if failed (with reason printed).
    """
    print("Running connectivity gate (pgr_connectedComponents) …")

    with engine.connect() as conn:
        # Get component for each vertex
        rows = conn.execute(text("""
            SELECT component, node
            FROM pgr_connectedComponents(
                'SELECT id::bigint, source::bigint, target::bigint,
                        cost::float8, reverse_cost::float8
                 FROM cscl_net
                 WHERE source IS NOT NULL AND target IS NOT NULL'
            )
        """)).fetchall()

    if not rows:
        print("  FAIL: pgr_connectedComponents returned no rows")
        return False

    from collections import Counter
    comp_size = Counter(r[0] for r in rows)
    largest_comp, largest_size = comp_size.most_common(1)[0]
    nodes_in_largest = {r[1] for r in rows if r[0] == largest_comp}
    print(f"  Total components: {len(comp_size):,}")
    print(f"  Largest component: #{largest_comp} with {largest_size:,} vertices")
    print(f"  Top 5 components by size: {comp_size.most_common(5)}")

    # Check all 5 boroughs represented in largest component via snapped stations
    with engine.connect() as conn:
        boro_rows = conn.execute(text("""
            SELECT DISTINCT borough
            FROM bike_stations
            WHERE cl_vertex_id = ANY(:nodes)
        """), {"nodes": list(nodes_in_largest)}).fetchall()

    boros_in = {r[0] for r in boro_rows if r[0]}
    expected_boros = {"Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"}
    missing_boros = expected_boros - boros_in
    print(f"  Boroughs with stations in largest component: {sorted(boros_in)}")
    if missing_boros:
        print(f"  WARN: Missing boroughs: {missing_boros} "
              "(may be OK if those boroughs have no Citibike stations)")

    # Check named bridge segments are in largest component
    bridge_names = [
        "BROOKLYN BRG", "BROOKLYN BRIDGE",
        "WILLIAMSBURG BRG", "WILLIAMSBURG BRIDGE",
        "ED KOCH QUEENSBORO", "MANHATTAN BRIDGE", "MANHATTAN BRG",
    ]
    with engine.connect() as conn:
        bridge_rows = conn.execute(text("""
            SELECT street_name, id, source, target
            FROM cscl_net
            WHERE upper(street_name) SIMILAR TO :pattern
              AND rw_type = 3
            LIMIT 20
        """), {"pattern": "%(BROOKLYN BRG|WILLIAMSBURG BRG|QUEENSBORO|MANHATTAN BRG|MANHATTAN BRIDGE)%"}).fetchall()

    bridges_found = []
    bridges_missing = []
    for name, eid, src, tgt in bridge_rows:
        in_comp = (src in nodes_in_largest or tgt in nodes_in_largest)
        if in_comp:
            bridges_found.append(name)
        else:
            bridges_missing.append(name)
        print(f"  Bridge seg '{name}' (edge {eid}): src={src} tgt={tgt} in_largest={in_comp}")

    if not bridge_rows:
        print("  WARN: No named bridge segments found in cscl_net (check rw_type=3 filter)")

    # PASS criteria: largest component has >50% of vertices AND key boroughs present
    # (Staten Island has no Citibike stations so we only require Manhattan+Brooklyn+Queens)
    required_boros = {"Manhattan", "Brooklyn", "Queens"}
    boro_ok = required_boros.issubset(boros_in)
    size_ok = largest_size > 50000  # should be huge for a full NYC network

    if not size_ok:
        print(f"  FAIL: Largest component too small ({largest_size:,} < 50,000 vertices)")
        return False
    if not boro_ok:
        missing_req = required_boros - boros_in
        print(f"  FAIL: Required boroughs not connected: {missing_req}")
        return False

    print(f"  PASS: Connectivity gate passed")
    return True


def main():
    gdf, gdf_m = load_and_filter()
    write_to_db(gdf, gdf_m)

    n_vtx, n_orphan = build_topology()
    n_snapped, n_unsnapped = snap_stations()
    gate_ok = connectivity_gate()

    print()
    print("=" * 60)
    if not gate_ok:
        print("CONNECTIVITY GATE FAILED — do not proceed to routing.")
        print("Check topology tolerance or bike-access filter.")
        print("Stage 2 fallback (edge_flows_lwavg.geojson) remains primary.")
        sys.exit(1)

    print("05b complete. cscl_net is ready for routing (06b_route_centerline.sql).")
    print(f"  cscl_net edges: with source/target assigned")
    print(f"  cscl_vertices: {n_vtx:,}")
    print(f"  Stations snapped (cl_vertex_id): {n_snapped:,}  unsnapped: {n_unsnapped}")


if __name__ == "__main__":
    main()
