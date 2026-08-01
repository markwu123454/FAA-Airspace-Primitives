# faa-airspace-primitives

FAA Class B, C, D, and E surface airspace boundaries as **compact arc-encoded primitives** derived via geodesic parameter recovery on the WGS84 ellipsoid.

Updated automatically every 28 days to match the FAA's publication cycle.

---

## Why this exists

FAA controlled airspace boundaries relevant to hobby aviation (0–400 ft AGL) need to be machine-readable as actual geometric shapes, not tessellated polygons.

The FAA publishes the CIFP in ARINC 424 fixed-width format, encoding boundaries as circles and arc segments. This repo goes further: it parses those records, tessellates complete circles to match real-world vertex distributions, then **recovers optimal arc parameters** via geodesic fitting on the WGS84 ellipsoid.

Why parameter recovery instead of direct extraction?
- **WGS84 accuracy** — spherical models have ~40m error on 5nm circles
- **Geodesic fitting** — finds the mathematically optimal circle through tessellated vertices
- **Validation** — every recovered arc is checked analytically; failures fall back to points
- **Compact encoding** — ~80% smaller than full vertex lists with no loss of precision

---

## Output

**`airspace.json`** — updated each 28-day cycle.

```jsonc
{
  "source":    "FAA CIFP 28-day cycle 260709",
  "format":    "arc-encoded primitives (geodesic fitting on WGS84)",
  "scope":     "All Class B/C/D airspace shells (CIFP UC subsection)",
  "count":     687,
  "by_class":  { "B": 43, "C": 115, "D": 527 },
  "generated": "2026-07-09T12:34:56Z",
  "airspaces": [...]
}
```

Each airspace entry:

```jsonc
{
  "id":         "AKBIL_PAC_A",
  "name":       "BILLINGS",
  "icao":       "KBIL",
  "class":      "C",
  "floor_ft":   0,         // feet MSL; 0 = surface
  "ceiling_ft": 7700,      // feet MSL
  "floor_ref":  "MSL",     // MSL or AGL
  "ceiling_ref": "MSL",
  "geometry": {
    "s": [lat, lon],       // ring start position
    "d": [...]             // primitives
  }
}
```

### Geometry format

A ring is encoded as a start position plus a list of primitives that **append** points after the current cursor:

```jsonc
"geometry": {
  "s": [45.807847, -108.543542],  // start: [lon, lat]
  "d": [
    ["a", -108.543542, 45.807847, 9260, 0, 360],    // arc
    ["l", -108.450000, 45.900000],                  // line segment
    ["p", -108.3, 45.8, -108.2, 45.75, -108.1, 45.7] // polyline points
  ]
}
```

**Primitive types:**
- `["a", lon, lat, radius_m, start_bearing_deg, sweep_deg]` — arc on WGS84 geodesic
- `["l", lon, lat]` — straight segment endpoint
- `["p", lon, lat, lon, lat, ...]` — polyline (point run simplified via Douglas-Peucker at 0.25m tolerance)

All coordinates are decimal degrees (WGS84), positive = N/E. Radii are in metres.

---

## Stats (May 2026 cycle)

| Class | Count | Notes |
|---|---|---|
| B | 43 | Surface shelves only (floor = 0) |
| C | 115 | Surface ring |
| D | 527 | Typically one circle |
| E | 2 | Surface extensions |
| **Total** | **687** | |

---

## Running it yourself

Install the single required dependency:

```bash
pip install geographiclib
```

Then run:

```bash
python3 parse_cifp.py
```

The script downloads the current CIFP zip from the FAA, parses `FAACIFP18`, recovers arc parameters via geodesic fitting, and writes `airspace.json`. Requires internet access.

### Options

```
--cycle YYMMDD      CIFP cycle date (default: auto-detect)
--cifp-file FILE    Use a local .zip or FAACIFP18 text file instead of downloading
--output FILE       Output path (default: airspace.json)
```

### Example with a local file

```bash
python3 parse_cifp.py --cifp-file /path/to/CIFP_260514.zip
python3 parse_cifp.py --cifp-file /path/to/FAACIFP18
```

---

## Algorithm

**Segmentation** — Turn-angle classification in local equirectangular metres:
1. Convert ring to local metres about its centroid
2. Calculate turn angle at each vertex
3. Group consecutive vertices into runs: constant-turn (arcs), straight (lines), or irregular (points)
4. Runs < 20 vertices are collapsed to points to avoid fitting overhead

**Arc Recovery** — Geodesic fitting on WGS84:
1. Sample ~48 vertices evenly across the run
2. Fit initial circle using Kasa algebraic method in planar coordinates
3. Refine centre via pattern search, minimizing geodesic radius spread
4. Calculate radius and bearing sweep, accounting for ±180° wraparound
5. Snap radius to FAA's 0.1nm regulatory grid (±1m tolerance)
6. **Validate**: every source vertex must be within 0.5m of recovered radius
7. Reject arcs that fail validation; fall back to point simplification

**Simplification** — Douglas-Peucker for point runs:
- Tolerance: 0.25 metres (tighter than most datasets afford, because arcs absorb ~93% of vertices)

**Validation** — Multi-level:
- Per-arc: analytical check against all source vertices during encoding
- Per-feature: sample of whole airspaces are expanded back through the decoder and compared to original vertices

---

## Data source

**FAA Aeronautical Data** — Coded Instrument Flight Procedures (CIFP)  
Published free, no login required, every 28 days.

URL pattern: `https://aeronav.faa.gov/Upload_313-d/cifp/CIFP_YYMMDD.zip`

Cycle dates listed at:  
https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/

The CIFP zip contains one file (`FAACIFP18`) in ARINC 424 fixed-width format.
`SUSAUC` records are the US controlled airspace boundary records.

### Filtering

Only airspaces with `floor_ft <= 400` are included. This captures:
- All surface-to-altitude airspaces (GND / SFC floor)
- Any shelf whose floor is at or below 400 ft MSL

Class E extensions starting at 700 ft or 1200 ft AGL are excluded.

### Known limitation

Class E4 instrument approach extensions (typically 4.1 NM circles at the surface)
are underrepresented in the CIFP. For complete E4 coverage, supplement with the
FAA ArcGIS Class_Airspace FeatureServer filtered to `LOCAL_TYPE='CLASS_E4'`.

---

## Upcoming cycle dates

- 2026-06-11
- 2026-07-09
- 2026-08-06
- 2026-09-03
- 2026-10-01
- 2026-10-29
- 2026-11-26
- 2026-12-24

The GitHub Action runs at 10:00 UTC on each date and commits the updated `airspace.json`.

---

## License

Code: MIT  
Data: FAA public domain
