"""
Build a clean flow network from the NYC Street Centerline (CSCL) and attribute
edge_flows trips via nearest-edge spatial SQL KNN.

Usage:
  python pipeline/08_clean_network.py           # full run
  python pipeline/08_clean_network.py --sample  # Flatiron/Broadway bbox only

--sample writes docs/data/edge_flows_sample.geojson for A/B visual inspection
against the previous v1 sample before running the full pipeline.
Full run builds nyc_centerline + clean_flows tables; run 07_export.py after.

Architecture: CSCL is one professionally maintained centerline per street
section — no dual carriageways, no algorithm artifacts. edge_flows midpoints
are snapped to their nearest centerline segment via PostGIS KNN; trips from
both OSM carriageways collapse onto the single CSCL line automatically.
"""

import argparse
import os
from pathlib import Path

import geopandas as gpd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DB_URL = (
    f"postgresql://{os.environ['PGUSER']}:{os.environ.get('PGPASSWORD', '')}"
    f"@{os.environ['PGHOST']}:{os.environ['PGPORT']}/{os.environ['PGDATABASE']}"
)
engine = create_engine(DB_URL)

# Accept either the canonical name or the dated download filename.
_CENTERLINE_CANDIDATES = [
    Path("data/raw/nyc_centerline.geojson"),
    Path("data/raw/Centerline_20260530.geojson"),
]
CENTERLINE_PATH = next((p for p in _CENTERLINE_CANDIDATES if p.exists()), _CENTERLINE_CANDIDATES[0])

OUT = Path("docs/data")
OUT.mkdir(parents=True, exist_ok=True)

SAMPLE_BBOX = (-73.995, 40.735, -73.982, 40.748)
MAX_SNAP_M = 30  # drop flow edges whose midpoint is >30 m from any CSCL segment


_ATTRIBUTION_SQL = [
    "DROP TABLE IF EXISTS clean_flows CASCADE",
    """
    CREATE TABLE clean_flows AS
    WITH assign AS (
      SELECT ef.total_trips, nn.cl_id
      FROM edge_flows ef
      CROSS JOIN LATERAL (
        SELECT c.cl_id,
               c.geom_m <-> ST_Transform(ST_LineInterpolatePoint(ef.geom, 0.5), 32618) AS d
        FROM nyc_centerline c
        ORDER BY c.geom_m <-> ST_Transform(ST_LineInterpolatePoint(ef.geom, 0.5), 32618)
        LIMIT 1
      ) nn
      WHERE nn.d < 30
    )
    SELECT c.cl_id AS id, c.geom, c.street_name,
           SUM(a.total_trips)::bigint AS total_trips
    FROM assign a
    JOIN nyc_centerline c ON c.cl_id = a.cl_id
    GROUP BY c.cl_id, c.geom, c.street_name
    """,
    "ALTER TABLE clean_flows ADD PRIMARY KEY (id)",
    "CREATE INDEX clean_flows_geom_idx ON clean_flows USING GIST(geom)",
]


def load_centerline(bbox=None):
    """Load CSCL, explode MultiLineString → LineString, optionally clip to bbox."""
    print(f"Loading {CENTERLINE_PATH} …")
    gdf = gpd.read_file(CENTERLINE_PATH)
    gdf = gdf.set_crs(4326, allow_override=True)
    gdf = gdf[["street_name", "geometry"]].copy()

    if bbox is not None:
        minx, miny, maxx, maxy = bbox
        gdf = gdf.cx[minx:maxx, miny:maxy]

    # CSCL ships as MultiLineString; explode to LineString for KNN indexing.
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    gdf = gdf[gdf.geometry.geom_type == "LineString"].copy()
    print(f"  {len(gdf):,} LineString segments")
    return gdf


def run_sample(bbox):
    minx, miny, maxx, maxy = bbox

    gdf_cl = load_centerline(bbox=bbox)
    gdf_cl_m = gdf_cl.to_crs(32618).copy()

    env = f"ST_MakeEnvelope({minx},{miny},{maxx},{maxy},4326)"
    print("Loading edge_flows in bbox …")
    gdf_flow = gpd.read_postgis(
        f"SELECT id, total_trips, geom FROM edge_flows WHERE geom && {env}",
        engine, geom_col="geom", crs=4326,
    ).to_crs(32618)
    print(f"  {len(gdf_flow):,} flow edges")

    print("Attributing trips via KNN …")
    midpoints = gdf_flow.geometry.interpolate(0.5, normalized=True)
    trip_sums = {}
    skipped = 0
    for mid, trips in zip(midpoints, gdf_flow["total_trips"]):
        dists = gdf_cl_m.geometry.distance(mid)
        idx = dists.idxmin()
        if dists[idx] < MAX_SNAP_M:
            trip_sums[idx] = trip_sums.get(idx, 0) + int(trips)
        else:
            skipped += 1

    gdf_cl_m["total_trips"] = gdf_cl_m.index.map(trip_sums).fillna(0).astype(int)
    result = (
        gdf_cl_m[gdf_cl_m["total_trips"] > 0][["street_name", "total_trips", "geometry"]]
        .copy()
        .to_crs(4326)
        .sort_values("total_trips", ascending=False)
        .reset_index(drop=True)
    )

    out_path = OUT / "edge_flows_sample.geojson"
    result.to_file(out_path, driver="GeoJSON")

    orig_trips = int(gdf_flow["total_trips"].sum())
    clean_trips = int(result["total_trips"].sum())
    dropped_pct = (orig_trips - clean_trips) / orig_trips * 100 if orig_trips else 0
    print(f"  {len(result):,} segments  →  {out_path}")
    print(f"  Trips: {clean_trips:,} / {orig_trips:,}  ({dropped_pct:.1f}% dropped by {MAX_SNAP_M}m guard)")
    if skipped:
        print(f"  ({skipped} flow edges had no centerline within {MAX_SNAP_M}m)")
    print("\nSample done. Eyeball docs/data/edge_flows_sample.geojson in the map.")


def run_full():
    gdf_cl = load_centerline()

    print("Loading borough boundaries and clipping …")
    boroughs = gpd.read_postgis(
        "SELECT geom FROM boroughs", engine, geom_col="geom", crs=4326,
    )
    nyc_union = boroughs.geometry.union_all()
    gdf_cl = gdf_cl[gdf_cl.intersects(nyc_union)].reset_index(drop=True)
    print(f"  {len(gdf_cl):,} segments within NYC")

    gdf_cl["cl_id"] = range(1, len(gdf_cl) + 1)
    gdf_write = gdf_cl[["cl_id", "street_name", "geometry"]].rename_geometry("geom")

    print(f"Writing nyc_centerline ({len(gdf_write):,} rows) to DB …")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS nyc_centerline CASCADE"))
    gdf_write.to_postgis("nyc_centerline", engine, if_exists="replace", index=False)

    print("  Adding geom_m column and GIST index …")
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE nyc_centerline ADD PRIMARY KEY (cl_id)"))
        conn.execute(text(
            "ALTER TABLE nyc_centerline ALTER COLUMN geom "
            "TYPE geometry(LineString, 4326) USING ST_SetSRID(geom, 4326)"
        ))
        conn.execute(text(
            "ALTER TABLE nyc_centerline ADD COLUMN geom_m geometry(LineString, 32618)"
        ))
        conn.execute(text("UPDATE nyc_centerline SET geom_m = ST_Transform(geom, 32618)"))
        conn.execute(text(
            "CREATE INDEX nyc_centerline_geom_m_idx ON nyc_centerline USING GIST(geom_m)"
        ))
    print("  nyc_centerline written and indexed")

    print("Attributing trips (spatial KNN SQL) …")
    with engine.begin() as conn:
        for sql in _ATTRIBUTION_SQL:
            conn.execute(text(sql))

    with engine.connect() as conn:
        orig_trips = conn.execute(text("SELECT SUM(total_trips) FROM edge_flows")).scalar()
        clean_trips = conn.execute(text("SELECT SUM(total_trips) FROM clean_flows")).scalar()
        clean_count = conn.execute(text("SELECT COUNT(*) FROM clean_flows")).scalar()

    dropped_pct = (orig_trips - clean_trips) / orig_trips * 100 if orig_trips else 0
    print(f"\nCenterline segments: {len(gdf_cl):,}")
    print(f"Clean flow segments: {clean_count:,}")
    print(f"Trips original:      {orig_trips:,}")
    print(f"Trips on clean:      {clean_trips:,}  ({dropped_pct:.1f}% dropped by {MAX_SNAP_M}m guard)")
    print("\nFull run done. Run `python pipeline/07_export.py` to regenerate edge_flows.geojson.")


def main():
    parser = argparse.ArgumentParser(description="Build clean network from CSCL, attribute trips")
    parser.add_argument("--sample", action="store_true", help="Run on Flatiron/Broadway bbox only")
    parser.add_argument(
        "--bbox", nargs=4, type=float,
        metavar=("MINX", "MINY", "MAXX", "MAXY"),
        default=SAMPLE_BBOX,
    )
    args = parser.parse_args()

    if args.sample:
        run_sample(tuple(args.bbox))
    else:
        run_full()


if __name__ == "__main__":
    main()
