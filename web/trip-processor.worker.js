// ════════════════════════════════════════════════════════════════════════════
// trip-processor.worker.js
//
// Off-main-thread builder for the deck.gl TripsLayer binary buffers.
//
// Receives, per window:  { id, geoms: string[], t0: Float32Array }
//   geoms[i] – the route geometry for trip i, encoded as polyline6
//   t0[i]    – real-time start, seconds since period_start
// Each trip's traversal duration is derived here from its route length at bike speed.
//
// Returns (with transferables, zero-copy):
//   { id, length, startIndices: Int32Array, positions: Float32Array, timestamps: Float32Array }
//
// positions are flat [lng, lat, lng, lat, …] (deck.gl order), timestamps are one
// per vertex. startIndices.length === length + 1 (last entry = total vertex count),
// which is the binary-attribute format TripsLayer/PathLayer expect.
//
// This replaces the old main-thread buildBinaryBuffers(): polyline decode + the
// per-vertex loop now run here so the UI thread never stalls.
// ════════════════════════════════════════════════════════════════════════════

// Fraction of each route (by distance) over which to ease in / out. Bikes
// accelerate from and decelerate into stations, so the animation lingers near
// the endpoints instead of moving at constant speed.
const EASE_FRAC = 0.15;

// Traversal duration is derived from route length at a fixed bike speed, so a bike
// visibly travels its route over a believable time (longer routes take longer) and
// only a short lit segment moves along the path. Tune BIKE_SPEED_MPS to speed bikes up/down.
const BIKE_SPEED_MPS = 3.5;     // ~12.6 km/h
const DEG_TO_M       = 111320;  // metres per degree of latitude (cum dist is in lat-degrees)
const MIN_DUR_S      = 30;      // floor so very short routes still animate

// ── Polyline6 decoder ─────────────────────────────────────────────────────────
// Standard Google/Mapbox polyline algorithm at precision 6 (factor 1e6).
// Returns flat [lng, lat, lng, lat, …] directly (deck.gl coordinate order),
// avoiding an intermediate array-of-pairs allocation.
function decodePolyline6(str) {
  const factor = 1e6;
  const len = str.length;
  // Worst case: every 2 chars → one delta; pre-size generously, trim at the end.
  const out = [];
  let index = 0, lat = 0, lng = 0;
  while (index < len) {
    let result = 1, shift = 0, b;
    do { b = str.charCodeAt(index++) - 63 - 1; result += b << shift; shift += 5; } while (b >= 0x1f);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);
    result = 1; shift = 0;
    do { b = str.charCodeAt(index++) - 63 - 1; result += b << shift; shift += 5; } while (b >= 0x1f);
    lng += (result & 1) ? ~(result >> 1) : (result >> 1);
    out.push(lng / factor, lat / factor); // [lng, lat]
  }
  return out;
}

// Map a normalised distance fraction (0..1 along the route) to a normalised time
// fraction (0..1 of trip duration). sqrt ease at both ends → slow near stations.
function timeFraction(df) {
  if (df < EASE_FRAC)            return EASE_FRAC * Math.sqrt(df / EASE_FRAC);
  if (df > 1 - EASE_FRAC)        return 1 - EASE_FRAC * Math.sqrt((1 - df) / EASE_FRAC);
  return df;
}

self.onmessage = (e) => {
  const { id, geoms, t0 } = e.data;
  const count = geoms.length;

  // ── Pass 1: decode all geometries, count valid trips and total vertices ──────
  const decoded = new Array(count); // flat [lng,lat,…] per trip, or null
  let validCount = 0;
  let totalVerts = 0;
  for (let i = 0; i < count; i++) {
    const g = geoms[i];
    if (!g) { decoded[i] = null; continue; }
    const flat = decodePolyline6(g);
    const nVerts = flat.length >> 1;
    if (nVerts < 2) { decoded[i] = null; continue; }
    decoded[i] = flat;
    validCount++;
    totalVerts += nVerts;
  }

  const positions    = new Float32Array(totalVerts * 2);
  const timestamps   = new Float32Array(totalVerts);
  const startIndices = new Int32Array(validCount + 1);

  // ── Pass 2: fill buffers, computing eased per-vertex timestamps ──────────────
  let trip = 0; // index into the compacted (valid-only) trip list
  let ptr  = 0; // running vertex pointer
  for (let i = 0; i < count; i++) {
    const flat = decoded[i];
    if (!flat) continue;
    const nVerts = flat.length >> 1;
    const t0i  = t0[i];

    // Cumulative chord length along the route, in degrees-of-latitude units
    // (equirectangular approximation with a cos(lat) correction on longitude).
    let cum = 0;
    const cumDist = new Float64Array(nVerts);
    for (let j = 1; j < nVerts; j++) {
      const dlng = flat[j * 2]     - flat[(j - 1) * 2];
      const dlat = flat[j * 2 + 1] - flat[(j - 1) * 2 + 1];
      const cl = Math.cos(flat[j * 2 + 1] * Math.PI / 180);
      cum += Math.sqrt(dlng * dlng * cl * cl + dlat * dlat);
      cumDist[j] = cum;
    }
    const total = cum > 0 ? cum : 1;

    // Real-time traversal duration from route length at bike speed.
    const distM = cum * DEG_TO_M;
    const duri  = Math.max(MIN_DUR_S, distM / BIKE_SPEED_MPS);

    startIndices[trip] = ptr;
    for (let j = 0; j < nVerts; j++) {
      positions[ptr * 2]     = flat[j * 2];
      positions[ptr * 2 + 1] = flat[j * 2 + 1];
      timestamps[ptr]        = t0i + duri * timeFraction(cumDist[j] / total);
      ptr++;
    }
    trip++;
  }
  startIndices[validCount] = ptr; // sentinel: total vertex count

  self.postMessage(
    { id, length: validCount, startIndices, positions, timestamps },
    [startIndices.buffer, positions.buffer, timestamps.buffer]
  );
};
