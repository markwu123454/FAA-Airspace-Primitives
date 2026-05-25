# faa-airspace-primitives

FAA Class B, C, and D airspace boundaries as geometric primitives — circles, circular arcs, and straight lines — rebuilt from the official NASR shapefile.

Updated automatically every 28 days to match the FAA's publication cycle.

---

## Why this exists

The FAA publishes class airspace boundaries in two forms:

- **Shapefile** (`class_airspace_shape_files.zip`): tessellated polygons. A simple Class D circle becomes 6,000+ vertices. Arc semantics are destroyed.
- **AIXM 5.1**: does not include class airspace at all. Only Navigation Aids, Airports, Airways, and ASOS/AWOS.

The old `CLS_ARSP.txt` legacy subscriber file preserved primitive geometry, but the FAA retired it without replacement.

The legal definitions in [FAA Order JO 7400.11](https://www.faa.gov/air_traffic/publications/) describe airspace as circles, arcs, and lines ("a 5 NM radius circle centered on..., then clockwise along a 10 NM arc to..."). This repo reconstructs that representation from the shapefile by fitting geometric primitives to the tessellated polygon vertices.

---

## Output

**`class_airspace_primitives.json`** — updated each 28-day NASR cycle.

```jsonc
{
  "source": "FAA NASR 28-day cycle effective 2026-05-14",
  "reconstructed_from": "Class_Airspace shapefile (polygons)",
  "filter": "Class B, C, D",
  "count": 1286,
  "airspaces": [...]
}
```

Each airspace object:

```jsonc
{
  "id": "SFO",
  "name": "SAN FRANCISCO CLASS B",
  "class": "B",
  "type_code": "CLASS",
  "local_type": "CLASS_B",
  "upper_value": "10000",   // ceiling altitude
  "upper_uom": "FT",
  "upper_code": "MSL",
  "upper_desc": "TI",       // TI = to indicated altitude
  "lower_value": "0",
  "lower_uom": "FT",
  "lower_code": "SFC",      // SFC = surface
  "comm_name": "",
  "mil_code": "CIV",
  "level": "L",             // L = low, H = high
  "working_hours_code": "H24",
  "working_hours_remark": "",
  "rings": [
    // Each ring is one closed boundary polygon.
    // Class B has multiple rings (one per altitude shelf).
    // Class C has two rings (inner/outer).
    // Class D usually has one ring.
    [
      // Segments in order, connecting end-to-end around the ring.
      { "type": "circle", "center": [-122.374, 37.619], "radius_nm": 5.0 },

      { "type": "arc",
        "center": [-122.374, 37.619],
        "radius_nm": 10.0,
        "start": [-122.187, 37.463],
        "end": [-122.558, 37.463],
        "start_bearing_deg": 161.3,
        "end_bearing_deg": 198.7,
        "direction": "CW",          // CW = clockwise, CCW = counter-clockwise
        "sweep_deg": 37.4 },

      { "type": "line",
        "from": [-122.558, 37.463],
        "to":   [-122.600, 37.500] }
    ]
  ]
}
```

All coordinates are `[longitude, latitude]` in decimal degrees (WGS84).  
Bearings are magnetic-north-up (0° = north, 90° = east).

### Segment types

| type | fields | notes |
|---|---|---|
| `circle` | `center`, `radius_nm` | Full 360° boundary. `rings` will have only this one segment. |
| `arc` | `center`, `radius_nm`, `start`, `end`, `start_bearing_deg`, `end_bearing_deg`, `direction`, `sweep_deg` | Partial arc. Follow `direction` from `start` to `end`. |
| `line` | `from`, `to` | Straight segment. |

---

## Stats (current cycle)

| Class | Airspaces | Circles | Arcs | Lines |
|---|---|---|---|---|
| B | 369 | — | majority | some |
| C | 340 | ~180 | remainder | few |
| D | 577 | ~286 (50%) | remainder | few |
| **Total** | **1,286** | **440** | **1,613** | **3,599** |

~50% of Class D airspaces decompose to a single circle. The other 50% have extensions (e.g. instrument approach corridors). Class B shelves are often polygonal by legal definition.

Most common arc radii: 5.0, 10.0, 4.0, 4.5, 15.0, 20.0 NM — matching FAA standard airspace design dimensions.

---

## Running it yourself

```bash
pip install pyshp

python reconstruct.py
```

The script downloads the current NASR shapefile, reconstructs primitives, and writes `class_airspace_primitives.json`. Requires internet access.

### Tuning

Edit the constants at the top of `reconstruct.py`:

```python
ARC_FIT_TOL_NM = 0.08     # max fit residual to count as arc (NM). Tighter = fewer false arcs.
MIN_ARC_SWEEP_DEG = 3.0   # arcs shorter than this are treated as lines.
MIN_ARC_RADIUS_NM = 0.5   # arcs tighter than this are treated as corner noise.
RADIUS_SNAP_TOL_NM = 0.15 # snap radius to nearest 0.5 NM if within this tolerance.
```

---

## Algorithm

1. Download `Class_Airspace.shp` from the FAA NASR 28-day subscription.
2. For each polygon ring, project to a local equirectangular plane in nautical miles.
3. Scan for arcs using a seed-and-expand strategy:
   - Take 32 consecutive vertices as a seed. Fit a circle using the Kasa algebraic method.
   - If residual < 0.08 NM, angular progression is monotonic, and radius is plausible, expand the window using exponential doubling + binary search to find the maximum length that still fits.
   - Require the resulting arc to sweep at least 3° (rejects degenerate fits on near-collinear points).
   - Mark vertices used, advance past the arc, repeat.
4. Remaining vertices become line segments, with collinear runs merged.
5. Adjacent arc segments with matching center and radius are coalesced.
6. Arcs spanning ≥ 350° are promoted to full circles.
7. Arc and circle radii are snapped to the nearest 0.5 NM if within 0.15 NM.

---

## FAA NASR cycle dates

The FAA publishes new data every 28 days. Upcoming effective dates:

- 2026-06-11
- 2026-07-09
- 2026-08-06
- 2026-09-03
- 2026-10-01
- 2026-10-29
- 2026-11-26
- 2026-12-24

The GitHub Action in this repo runs the day each cycle goes live and commits the updated JSON.

---

## Data source

**FAA Aeronautical Data** — 28-Day NASR Subscription  
https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/

Shapefile: `class_airspace_shape_files.zip` under each cycle's page.

The FAA publishes this data for free with no restrictions on use.

---

## License

Code: MIT  
Data: FAA public domain
