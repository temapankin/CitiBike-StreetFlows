# CitiBike routing pipeline
# Usage:
#   make env      – create conda environment
#   make db-init  – initialise per-project Postgres cluster
#   make data     – download raw data (CSVs, OSM, boroughs)
#   make db       – load + clean trips, build stations table
#   make profile  – run feasibility probe (choose N for routing)
#   make route    – build street network + run pgRouting
#   make export   – write GeoJSON / timelapse JSON to web/data/
#   make serve    – serve web/ on http://localhost:8000
#   make all      – data → db → profile → route → export

SHELL := bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

include .env
export

CONDA_ACTIVATE = source $$(conda info --base)/etc/profile.d/conda.sh && conda activate citibike

PSQL = psql -v ON_ERROR_STOP=1

# ─── environment ───────────────────────────────────────────────────────────────

.PHONY: env
env:
	conda env create -f environment.yml || conda env update -f environment.yml --prune

# ─── database cluster (per-project, no sudo) ───────────────────────────────────

.PHONY: db-init
db-init:
	$(CONDA_ACTIVATE)
	if [ ! -d "$$PGDATA" ]; then
	    initdb -D "$$PGDATA" --username=$$PGUSER --pwfile=<(echo $$PGPASSWORD) --auth=md5
	fi
	pg_ctl -D "$$PGDATA" -l "$$PGDATA/postgres.log" -o "-p $$PGPORT" start || true
	sleep 2
	createdb -h $$PGHOST -p $$PGPORT -U $$PGUSER $$PGDATABASE 2>/dev/null || true
	$(PSQL) -c "ALTER USER $$PGUSER PASSWORD '$$PGPASSWORD';" 2>/dev/null || true

.PHONY: db-stop
db-stop:
	$(CONDA_ACTIVATE)
	pg_ctl -D "$$PGDATA" stop || true

# ─── data acquisition ──────────────────────────────────────────────────────────

data/raw/.download_done:
	$(CONDA_ACTIVATE)
	python pipeline/01_download_data.py
	touch $@

.PHONY: data
data: data/raw/.download_done

# ─── database loading + cleaning ───────────────────────────────────────────────

.db_loaded: data/raw/.download_done
	$(CONDA_ACTIVATE)
	$(PSQL) -f pipeline/00_setup.sql
	python pipeline/02_load_trips.py
	$(PSQL) -f pipeline/03_stations.sql
	$(PSQL) -f pipeline/04_trips.sql
	touch $@

.PHONY: db
db: .db_loaded

# ─── profiling ─────────────────────────────────────────────────────────────────

.PHONY: profile
profile: .db_loaded
	$(CONDA_ACTIVATE)
	python profile_data.py

# ─── routing ───────────────────────────────────────────────────────────────────

.route_done: .db_loaded
	$(CONDA_ACTIVATE)
	$(PSQL) -f pipeline/05_network.sql
	$(PSQL) -f pipeline/06_route_od.sql
	touch $@

.PHONY: route
route: .route_done

# ─── clean network (CSCL + trip attribution) ───────────────────────────────────

.clean_done: .route_done
	$(CONDA_ACTIVATE)
	python pipeline/08_clean_network.py
	touch $@

.PHONY: clean-network
clean-network: .clean_done

# ─── export ────────────────────────────────────────────────────────────────────

web/data/edge_flows.geojson: .clean_done
	$(CONDA_ACTIVATE)
	python pipeline/07_export.py

.PHONY: export
export: web/data/edge_flows.geojson

# ─── centerline network (cscl_net build + routing) ────────────────────────────
# Additive path: OSM pipeline is unchanged and still runnable via make route/export.
# Run these steps after `make route` (they need od_pairs and bike_stations with vertex_id).

.cscl_net_done: .db_loaded
	$(CONDA_ACTIVATE)
	python pipeline/05b_centerline_network.py
	touch $@

.PHONY: centerline-network
centerline-network: .cscl_net_done

.cscl_routed: .cscl_net_done
	$(CONDA_ACTIVATE)
	python pipeline/06b_route_centerline_batch.py
	touch $@

.PHONY: centerline-route
centerline-route: .cscl_routed

web/data/edge_flows_cl.geojson: .cscl_routed
	$(CONDA_ACTIVATE)
	FLOW_TABLE=edge_flows_cl SKIP_TIMELAPSE=1 python pipeline/07_export.py

.PHONY: centerline-export
centerline-export: web/data/edge_flows_cl.geojson

# Count-de-inflation variants from OSM edge_flows (fallback, no reroute needed)
.PHONY: aggregate-variants
aggregate-variants: .clean_done
	$(CONDA_ACTIVATE)
	python pipeline/08b_aggregate_variants.py

# Full centerline pipeline (network → route → export)
.PHONY: centerline
centerline: centerline-network centerline-route centerline-export

# ─── timelapse Parquet export ──────────────────────────────────────────────────
# Depends on cscl_routed (od_routes_cl must exist).

.timelapse_done: .cscl_routed
	$(CONDA_ACTIVATE)
	$(PSQL) -f pipeline/09_timelapse.sql
	python pipeline/10_export_parquet.py
	touch $@

.PHONY: timelapse
timelapse: .timelapse_done

# ─── vector tiles (PMTiles) ───────────────────────────────────────────────────

web/data/edge_flows.pmtiles: web/data/edge_flows.geojson
	$(CONDA_ACTIVATE)
	tippecanoe -o web/data/edge_flows.pmtiles \
	  --minimum-zoom=10 --maximum-zoom=14 \
	  --no-feature-limit --no-tile-size-limit \
	  -l flows web/data/edge_flows.geojson

.PHONY: tiles
tiles: web/data/edge_flows.pmtiles

# ─── web server ────────────────────────────────────────────────────────────────

.PHONY: serve
serve:
	$(CONDA_ACTIVATE)
	cd web && python server.py 8000

# ─── all ───────────────────────────────────────────────────────────────────────

.PHONY: all
all: data db profile route clean-network export timelapse tiles
