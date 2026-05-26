# faa-airspace-primitives

FAA Class B, C, D, and E surface airspace boundaries as geometric primitives — circles and polygon/arc chains — parsed directly from the official FAA CIFP (Coded Instrument Flight Procedures) file.

Updated automatically every 28 days to match the FAA's publication cycle.

---

## Why this exists

FAA controlled airspace boundaries relevant to hobby aviation (0–400 ft AGL) need to be machine-readable as actual geometric shapes, not tessellated polygons.

The FAA publishes the CIFP in ARINC 424 fixed-width format, which encodes boundaries **directly** as:

- **CE** — complete circle (center + radius)
- **G / GE** — great-circle points (straight boundary segments)
- **R** — arc segments (center, radius, direction)

This repo parses that file and emits the geometry as-is, with no reconstruction or fitting required. The result is exact: the same circles and arcs the FAA used to define the airspace.

---

## Output

**`airspace.json`** — updated each 28-day cycle.

```jsonc
{
  "source":   "FAA CIFP 28-day cycle 260514",
  "format":   "ARINC 424 (FAACIFP18)",
  "filter":   "Class B/C/D/E surface (floor_ft <= 400)",
  "count":    687,
  "by_class": { "B": 43, "C": 115, "D": 527, "E": 2 },
  "airspaces": [...]
}
```

Each airspace entry:

```jsonc
{
  "id":         "AKBIL_PAC_A",      // airport_cifpid_artcc_shell
  "name":       "BILLINGS",
  "airport":    "AKBIL",            // CIFP internal ID (strip leading char for ICAO)
  "icao":       "KBIL",             // standard ICAO identifier
  "class":      "C",                // B / C / D / E
  "floor_ft":   0,                  // feet MSL; 0 = surface
  "ceiling_ft": 7700,               // feet MSL
  "geometry":   { ... }
}
```

### Circle geometry

A single complete-circle boundary (most Class D and many Class C):

```jsonc
{
  "type":       "circle",
  "center_lat": 45.807847,
  "center_lon": -108.543542,
  "radius_nm":  5.0
}
```

### Polygon/arc geometry

A boundary built from a sequence of points and arc segments. The previous segment's end is the implicit start of each next segment:

```jsonc
{
  "type": "polygon_arc",
  "segments": [
    { "type": "point", "lat": 45.12, "lon": -108.34 },

    { "type": "arc",
      "end_lat":    45.23,   "end_lon":    -108.45,
      "center_lat": 45.10,   "center_lon": -108.30,
      "radius_nm":  5.0,
      "direction":  "R" },   // R = clockwise, L = counterclockwise

    { "type": "point", "lat": 45.30, "lon": -108.50 }
  ]
}
```

All coordinates are decimal degrees (WGS84), positive = N/E.

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

No dependencies beyond the Python standard library:

```bash
python3 parse_cifp.py
```

The script downloads the current CIFP zip from the FAA, parses `FAACIFP18`, and writes `airspace.json`. Requires internet access.

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
